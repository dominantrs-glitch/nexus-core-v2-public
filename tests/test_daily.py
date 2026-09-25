from datetime import datetime
import unittest
from unittest.mock import patch
from nexus.daily import collect, render_mail, unavailable
from nexus.desktop_bridge import dispatch


class DailyTests(unittest.TestCase):
    def test_calendar_output_distinguishes_current_events_and_unavailable_source(self):
        v = unavailable('morning', datetime.fromisoformat('2026-09-24T08:00:00+09:00'))
        v.update(calendar_status='current', events=[dict(title='面談 <確認>', start='2026-09-24T01:00:00Z',
                 end='2026-09-24T02:00:00Z', all_day=False), dict(title='記念日', all_day=True)])
        mail = render_mail(v)
        self.assertIn('09-24 10:00〜09-24 11:00 面談 <確認>', mail['text'])
        self.assertIn('終日 記念日', mail['text'])
        self.assertIn('面談 &lt;確認&gt;', mail['html'])
        v.update(calendar_status='stale', calendar_message='予定の再取得が必要です。')
        stale = render_mail(v)
        self.assertIn('予定の再取得が必要です。', stale['text'])
        self.assertNotIn('面談', stale['text'])
        self.assertNotIn('予定はありません', stale['text'])

    def test_failure_removes_previous_focus_and_marks_data_unavailable(self):
        class Failed:
            def _call(self, *_):
                raise ValueError('sensitive upstream details')
        v = collect('desktop', client=Failed(), at=datetime.fromisoformat('2026-09-24T23:59:00+09:00'))
        self.assertEqual(v['hours_date'], '2026-09-25')
        self.assertEqual(v['state'], 'unavailable')
        self.assertIsNone(v['focus'])
        self.assertNotIn('sensitive', str(v))

    def test_bridge_daily_is_read_only_and_uses_the_desktop_surface(self):
        with patch('nexus.daily.collect', return_value={'schema': 1}) as fn:
            w = object()
            self.assertEqual(dispatch({'action': 'daily'}, w), {'schema': 1})
            fn.assert_called_once_with('desktop', workspace=w)

    def test_html_escapes_record_text_and_preserves_every_alert(self):
        class Client:
            def _call(self, op, args):
                self.args = args
                return dict(schema=1, complete=True, surface='morning', local_date='2026-09-24', hours_date='2026-09-24',
                            state='partial', as_of=args['now'], hours_status='missing', hours_text='今日の勤務：勤務時間未取得',
                            focus={'title': '<script>alert(1)</script>', 'reason': 'AIの着手案'},
                            actions=[], alerts=[{'title': str(i), 'text': '待機'} for i in range(8)], projects=[], limitation='Scope')
        client = Client()
        v = collect(client=client, at=datetime.fromisoformat('2026-09-24T06:00:00+09:00'))
        m = render_mail(v)
        self.assertNotIn('<script>', m['html'])
        self.assertIn('&lt;script&gt;', m['html'])
        self.assertIn('7：待機', m['text'])
        self.assertFalse(m['send_enabled'])

    def test_wrong_surface_or_date_is_not_accepted_as_current_data(self):
        class Wrong:
            def _call(self, *_):
                return dict(schema=1, complete=True, surface='desktop', state='ok', local_date='2026-09-23', hours_date='2026-09-24')
        v = collect('desktop', client=Wrong(), at=datetime.fromisoformat('2026-09-24T22:00:00+09:00'))
        self.assertEqual(v['state'], 'unavailable')
        with self.assertRaises(ValueError):
            render_mail(v)


if __name__ == '__main__':
    unittest.main()
