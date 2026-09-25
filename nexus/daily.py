"""Read-only daily view shared by Desktop and unsent morning-mail previews."""
import argparse
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import sys

from nexus.git_intake import GitIntake, RoutedIntake, open_intake
from nexus.intake import default_root

JST = timezone(timedelta(hours=9))


def local_client(workspace=None):
    workspace = workspace or open_intake(default_root())
    if isinstance(workspace, GitIntake):
        return workspace
    if isinstance(workspace, RoutedIntake):
        # Only an existing owner-configured route is eligible. No new enrollment.
        with workspace.db() as db:
            rows = db.execute('SELECT project,config_root,generation FROM intake_routes').fetchall()
        identities = {(r['config_root'], r['generation']) for r in rows}
        if len(identities) != 1:
            raise ValueError('one configured shared source required')
        return workspace._target(rows[0]['project'])
    raise ValueError('shared source unavailable')


def unavailable(surface, at):
    date = at.date() + timedelta(days=surface == 'desktop')
    return dict(schema=1, surface=surface, state='unavailable', as_of=at.isoformat(),
                local_date=at.date().isoformat(), hours_date=date.isoformat(), expires_at=at.isoformat(),
                hours_status='unavailable', hours_text=('明日' if surface == 'desktop' else '今日') + 'の勤務：勤務時間未取得',
                focus=None, actions=[], alerts=[], projects=[], hours_records=[], complete=False,
                limitation='情報を取得できませんでした。前回の内容は現在の情報として表示していません。再取得してください。',
                snapshot=None, binding=False)


def collect(surface='morning', *, client=None, workspace=None, at=None):
    if surface not in ('morning', 'desktop'):
        raise ValueError('unsupported surface')
    at = (at or datetime.now(JST)).astimezone(JST)
    try:
        client = client or local_client(workspace)
        result = client._call('daily', dict(surface=surface, now=at.isoformat(timespec='seconds'), utc_offset='+09:00'))
        expected_date = (at.date() + timedelta(days=surface == 'desktop')).isoformat()
        if (result.get('schema') != 1 or result.get('complete') is not True or
                result.get('surface') != surface or result.get('hours_date') != expected_date or
                result.get('local_date') != at.date().isoformat() or
                result.get('state') not in ('ok', 'partial')):
            raise ValueError('invalid daily response')
        return result
    except Exception:
        # No credential-bearing upstream text, stale cache or partial focus is returned.
        return unavailable(surface, at)


def render_mail(view):
    if view.get('surface') != 'morning':
        raise ValueError('morning surface required')
    title = view['local_date'] + ' 朝の確認'
    if view['state'] == 'unavailable':
        title += '（情報取得できず）'
    lines = [title, '', view['hours_text']]
    sections = []
    if view['hours_status'] == 'conflict':
        lines.extend('勤務の記録：' + row['start'] + '〜' + row['end'] for row in view['hours_records'])
    focus = view.get('focus')
    if focus:
        lines += ['', '最初の着手候補', focus['title'], focus['reason']]
        sections.append(('最初の着手候補', [focus['title'], focus['reason']]))
    elif view['state'] != 'unavailable':
        lines += ['', 'この保存範囲には、すぐ着手できる作業が登録されていません。']
        sections.append(('着手候補', ['この保存範囲には、すぐ着手できる作業が登録されていません。']))
    following = view.get('actions', [])[1:]
    if following:
        rows = [x['title'] + ('（期限 ' + x['due_at'] + '）' if x.get('due_at') else '') for x in following]
        lines += ['', '次の候補', *rows]
        sections.append(('次の候補', rows))
    if 'calendar_status' in view:
        if view['calendar_status'] == 'current':
            def event_text(event):
                def stamp(value):
                    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(JST).strftime('%m-%d %H:%M')
                when = '終日' if event['all_day'] else stamp(event['start']) + '〜' + stamp(event['end'])
                return when + ' ' + event['title']
            rows = [event_text(event) for event in view.get('events', [])] or ['予定はありません。']
        else:
            rows = [view.get('calendar_message') or '予定を取得できませんでした。']
        lines += ['', '今日の予定', *rows]
        sections.append(('今日の予定', rows))
    if view.get('alerts'):
        rows = [x['title'] + '：' + x['text'] for x in view['alerts']]
        lines += ['', '確認・注意', *rows]
        sections.append(('確認・注意', rows))
    if view.get('projects'):
        rows = [x['title'] + '：' + x['summary'] for x in view['projects']]
        lines += ['', '関連する案件', *rows]
        sections.append(('関連する案件', rows))
    footer = '取得時刻：' + view['as_of'] + '\n' + view['limitation']
    lines += ['', footer]
    esc = html.escape
    body = ('<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="margin:0;background:#f5f3f8;color:#282334;font:16px/1.7 sans-serif">'
            '<main style="max-width:640px;margin:28px auto;padding:28px;background:#fff;border-radius:18px">'
            '<div style="font-size:12px;letter-spacing:2px;color:#75628e">NEXUS · MORNING</div>'
            '<h1 style="font-size:25px;margin:8px 0 22px">' + esc(title) + '</h1>'
            '<div style="padding:18px;background:#f0eaf8;border-radius:12px;font-size:20px;font-weight:700">' + esc(view['hours_text']) + '</div>')
    if view['hours_status'] == 'conflict':
        body += '<p>' + '<br>'.join(esc(x['start'] + '〜' + x['end']) for x in view['hours_records']) + '</p>'
    for heading, rows in sections:
        body += '<section style="margin-top:26px"><h2 style="font-size:14px;color:#75628e">' + esc(heading) + '</h2>'
        body += ''.join('<p style="margin:10px 0;overflow-wrap:anywhere">' + esc(row) + '</p>' for row in rows) + '</section>'
    body += '<footer style="margin-top:30px;padding-top:18px;border-top:1px solid #e6dfed;color:#70677b;font-size:12px;white-space:pre-wrap">' + esc(footer) + '</footer></main></body></html>'
    text = '\n'.join(lines) + '\n'
    if len(text.encode()) + len(body.encode()) > 180000:
        raise ValueError('mail too large; review all alerts before delivery')
    return dict(subject=title, text=text, html=body, send_enabled=False)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Existing trusted canonical route only')
    parser.add_argument('--surface', choices=['morning', 'desktop'], default='morning')
    parser.add_argument('--output', type=Path, help='Write an unsent local JSON/text/HTML preview')
    args = parser.parse_args()
    view = collect(args.surface, client=GitIntake(args.root) if args.root else None)
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / (args.surface + '.json')).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding='utf-8')
        if args.surface == 'morning':
            mail = render_mail(view)
            for suffix in ('html', 'text'):
                (args.output / ('morning.' + ('txt' if suffix == 'text' else suffix))).write_text(mail[suffix], encoding='utf-8')
    print(json.dumps(view, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
