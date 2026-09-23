"""Same-store work capture and a derived, read-only daily brief."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from nexus.git_intake import GitIntake


def read_all(client, *, project=None, at=None):
    now = at or datetime.now().astimezone()
    if now.utcoffset() is None:
        raise ValueError('explicit local time offset required')
    instant = now.isoformat(timespec='seconds')
    args = dict(date=now.date().isoformat(), now=instant, utc_offset=instant[-6:])
    if project is not None:
        args['project'] = project
    result = dict(items=[], hours=[], alerts=[], pages=0, complete=False, scope='permitted_shared_work_records')
    seen = set()
    while True:
        page = client.work(**args)
        if result['pages'] and page['snapshot'] != result['snapshot']:
            raise ValueError('work snapshot changed; restart')
        for key in ('items', 'hours', 'alerts'):
            result[key].extend(page[key])
        result.update(snapshot=page['snapshot'], scope=page['scope'], date=args['date'])
        result['pages'] += 1
        cursor = page['next_cursor']
        if cursor is None:
            if page.get('exhausted') is not True:
                raise ValueError('work listing incomplete')
            result['complete'] = True
            return result
        if cursor in seen or result['pages'] >= 1000:
            raise ValueError('work listing did not complete')
        seen.add(cursor)
        args.update(cursor=cursor, snapshot=page['snapshot'])


def brief(view):
    if not view.get('complete'):
        raise ValueError('complete work view required before briefing')
    hours = {(h['start'], h['end']) for h in view['hours']}
    if not hours:
        lines = ['勤務時間未取得です。']
    elif len(hours) > 1:
        lines = ['勤務時間の記録が一致していません。確認が必要です。']
        lines.extend(f'勤務の記録：{start} 〜 {end}' for start, end in sorted(hours))
    else:
        start, end = next(iter(hours))
        lines = [f'勤務の記録は {start} 〜 {end} です。']
    # This ordering is a view suggestion, not a change to the owner's priority.
    available = [i for i in view['items'] if i['type'] == 'action' and i['status'] == 'open'
                 and not i['blocker'] and not i['approval_wait']]
    available.sort(key=lambda i: (not i['overdue'], {'high': 0, 'normal': 1, None: 2, 'low': 3}[i['user_priority']],
                                  i['due_at'] or '9999', i['created_at'], i['id']))
    if available:
        lines.append('まずは「' + available[0]['title'] + '」から進める案です。')
        lines.append('次の作業：' + '、'.join('「' + i['title'] + '」' for i in available[:3]) + '。')
    else:
        lines.append('この保存範囲に、すぐ着手できる作業は登録されていません。')
    # Alerts are never cut to the three-action display limit.
    for item in view['alerts']:
        details = []
        if item['overdue']:
            details.append('期限を過ぎています')
        if item['blocker']:
            details.append('進められない理由：' + item['blocker'])
        if item['approval_wait']:
            details.append('本人の確認待ち')
        if item['waiting_for']:
            details.append('待っているもの：' + item['waiting_for'])
        if not details:
            details.append('待機中')
        lines.append('「' + item['title'] + '」：' + '／'.join(details) + '。')
    candidates = sum(i['status'] == 'candidate' for i in view['items'])
    intents = sum(i['type'] == 'intent' and i['status'] == 'open' for i in view['items'])
    if candidates or intents:
        lines.append(f'未確定の候補は{candidates}件、残しているやりたいことは{intents}件です。')
    return dict(text='\n'.join(lines), snapshot=view['snapshot'], date=view['date'], binding=False,
        scope=view['scope'], action_suggestions=[dict(project=i['project'], id=i['id']) for i in available[:3]],
        alert_count=len(view['alerts']), hours_status='missing' if not hours else 'conflict' if len(hours)>1 else 'reported',
        limitation='登録された共有の作業記録だけを表示しています。過去の文章中にある未整理の作業は含みません。')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Shared work records; trusted existing Git root required')
    parser.add_argument('--root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('read', 'brief'):
        command = sub.add_parser(name)
        command.add_argument('--project')
        command.add_argument('--at', help='ISO timestamp with explicit offset; default local current time')
    for name in ('save', 'hours'):
        sub.add_parser(name).add_argument('file', type=Path)
    args = parser.parse_args()
    client = GitIntake(args.root)
    if args.command in ('read', 'brief'):
        view = read_all(client, project=args.project, at=datetime.fromisoformat(args.at) if args.at else None)
        result = brief(view) if args.command == 'brief' else view
    else:
        payload = json.loads(args.file.read_text(encoding='utf-8-sig'))
        result = (client.save_work if args.command == 'save' else client.save_hours)(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
