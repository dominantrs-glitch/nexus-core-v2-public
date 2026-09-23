from datetime import datetime
import unittest
from nexus.work import brief, read_all


class Client:
    def __init__(self, pages):
        self.pages, self.calls = iter(pages), []

    def work(self, **args):
        self.calls.append(args)
        return next(self.pages)


def page(**extra):
    return dict(items=[], hours=[], alerts=[], snapshot='a'*40, scope='project_work_records',
                next_cursor=None, exhausted=True, **extra)


class WorkBriefTests(unittest.TestCase):
    def test_all_pages_are_required_and_snapshot_filter_kept(self):
        first = page()
        first.update(next_cursor='next', exhausted=False)
        client = Client([first, page()])
        view = read_all(client, at=datetime.fromisoformat('2026-09-23T12:00:00+09:00'))
        self.assertEqual(view['pages'], 2)
        self.assertEqual(client.calls[1]['snapshot'], 'a'*40)
        self.assertEqual(client.calls[0]['now'], client.calls[1]['now'])
        self.assertIn('勤務時間未取得', brief(view)['text'])
        with self.assertRaises(ValueError):
            brief(dict(view, complete=False))

    def test_conflicting_hours_and_all_alerts_survive_short_display(self):
        view = read_all(Client([page()]), at=datetime.fromisoformat('2026-09-23T12:00:00+09:00'))
        view['hours'] = [dict(start='2026-09-23T09:00:00+09:00', end='2026-09-23T17:00:00+09:00'),
                         dict(start='2026-09-23T10:00:00+09:00', end='2026-09-23T18:00:00+09:00')]
        view['items'] = [dict(type='action', status='open', blocker='', approval_wait=False,
            overdue=True, user_priority=None, due_at='2026-09-22', created_at='2026-09-21',
            project='p', id=str(n), title='Action '+str(n)) for n in range(6)]
        view['alerts'] = [dict(title=i['title'], overdue=True, blocker='', approval_wait=False, waiting_for='') for i in view['items']]
        result = brief(view)
        self.assertEqual(result['hours_status'], 'conflict')
        self.assertEqual(len(result['action_suggestions']), 3)
        self.assertEqual(result['alert_count'], 6)
        self.assertIn('Action 5', result['text'])

    def test_failure_or_snapshot_drift_is_not_an_empty_brief(self):
        first = page()
        first.update(next_cursor='next', exhausted=False)
        second = page()
        second.update(snapshot='b'*40)
        with self.assertRaises(ValueError):
            read_all(Client([first, second]))


if __name__ == '__main__':
    unittest.main()
