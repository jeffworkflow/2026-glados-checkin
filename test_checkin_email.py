"""Run with: python -m unittest test_checkin_email.py (no network/mail sent)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import checkin


class DashboardEmailTest(unittest.TestCase):
    def test_success_and_rejection(self):
        for response in (
            {'code': 0, 'message': 'Checkin! Got 10 Points'},
            {'code': 0, 'message': "Today's observation logged. Return tomorrow for more points."},
            {'code': 1, 'message': "Today's observation logged. Return tomorrow for more points."},
            {'code': '1', 'message': "Today's observation logged. Return tomorrow for more points."},
            {'code': 1, 'message': 'Checkin Repeats! Please Try Tomorrow'},
        ):
            self.assertTrue(checkin.checkin_succeeded(response))
        for response in (None, {}, {'code': 1, 'message': 'Checkin failed'},
                         {'code': 401, 'message': 'Checkin! Got 10 Points'},
                         {'message': 'Automated check-in detected'}):
            self.assertFalse(checkin.checkin_succeeded(response))

    def test_dashboard_data_and_secret_allowlist(self):
        g = checkin.GLaDOS('private-cookie')
        with patch.object(g, 'req', return_value={'code': 0, 'data': {
            'email': 'test@example.com', 'leftDays': '27.9', 'vip': 21,
            'traffic': 24411923538, 'password': 'private-subscription-password',
        }}):
            self.assertTrue(g.get_status())
        self.assertEqual((g.plan, g.traffic_limit, g.used_traffic), ('Basic', '200 GB', '22.74 GB'))
        report = dict(email=g.email, plan=g.plan, used_traffic=g.used_traffic,
                      traffic_limit=g.traffic_limit, left_days=g.left_days, points='20',
                      result='<script>alert(1)</script>', password='private-subscription-password')
        message = checkin.dashboard_mail([report])
        html = message.get_body(preferencelist=('html',)).get_content()
        self.assertEqual(message['To'], '418796833@qq.com')
        self.assertIn('北京时间', message.get_body(preferencelist=('plain',)).get_content())
        self.assertIn('22.74 GB', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('private-subscription-password', message.as_string())
        self.assertNotIn('private-cookie', message.as_string())

    def test_export_before_other_notification_early_return(self):
        with tempfile.TemporaryDirectory() as folder:
            report_file = Path(folder) / 'dashboard.json'
            with patch.dict(os.environ, {'DASHBOARD_REPORT_PATH': str(report_file), 'PUSH_LEVEL': 'fail_only'}, clear=True), \
                 patch.object(checkin, 'get_cookies', return_value=['private-cookie']), \
                 patch.object(checkin.GLaDOS, 'checkin', return_value={'code': 1, 'message': "Today's observation logged. Return tomorrow for more points."}), \
                 patch.object(checkin.GLaDOS, 'get_status', return_value=True), \
                 patch.object(checkin.GLaDOS, 'get_points', return_value=True):
                checkin.main()
            reports = json.loads(report_file.read_text(encoding='utf-8'))
            self.assertEqual(len(reports), 1)
            self.assertNotIn('private-cookie', report_file.read_text(encoding='utf-8'))

    def test_rejected_checkin_exports_nothing_and_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            report_file = Path(folder) / 'dashboard.json'
            report_file.write_text('[{"stale":true}]', encoding='utf-8')
            with patch.dict(os.environ, {'DASHBOARD_REPORT_PATH': str(report_file)}, clear=True), \
                 patch.object(checkin, 'get_cookies', return_value=['private-cookie']), \
                 patch.object(checkin.GLaDOS, 'checkin', return_value={'code': 401, 'message': 'Login expired'}), \
                 patch.object(checkin.GLaDOS, 'get_status', return_value=False), \
                 patch.object(checkin.GLaDOS, 'get_points', return_value=False):
                with self.assertRaises(SystemExit):
                    checkin.main()
            self.assertEqual(json.loads(report_file.read_text(encoding='utf-8')), [])
            with patch.dict(os.environ, {'DASHBOARD_REPORT_PATH': str(report_file)}, clear=True), \
                 patch.object(checkin.smtplib, 'SMTP_SSL') as smtp:
                checkin.send_dashboard_email()
                smtp.assert_not_called()

    def test_smtp_requires_secret_and_propagates_send_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            report_file = Path(folder) / 'dashboard.json'
            report_file.write_text(json.dumps([dict(email='test@example.com', plan='Basic',
                used_traffic='1 GB', traffic_limit='200 GB', left_days='27', points='20', result='Checkin! Got 10 Points')]), encoding='utf-8')
            with patch.dict(os.environ, {'DASHBOARD_REPORT_PATH': str(report_file)}, clear=True):
                with self.assertRaises(RuntimeError):
                    checkin.send_dashboard_email()
            with patch.dict(os.environ, {'DASHBOARD_REPORT_PATH': str(report_file), 'SMTP_PASSWORD': 'test-only-password'}, clear=True), \
                 patch.object(checkin.smtplib, 'SMTP_SSL') as smtp:
                connection = smtp.return_value.__enter__.return_value
                connection.send_message.return_value = {}
                checkin.send_dashboard_email()
                connection.login.assert_called_once_with('418796833@qq.com', 'test-only-password')
                self.assertEqual(connection.send_message.call_args.args[0]['To'], '418796833@qq.com')
                connection.send_message.side_effect = OSError('offline')
                with self.assertRaises(OSError):
                    checkin.send_dashboard_email()


if __name__ == '__main__':
    unittest.main()
