"""Current-user background connection. No listener, credential log or elevation."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from nexus.intake import default_root
from nexus.connection_diagnostics import REASONS, STATES, clean_events, clean_issue, issue

BASE = Path(__file__).resolve().parents[1]


def control_root():
    return Path(os.environ['LOCALAPPDATA']) / 'NexusCoreV2' / 'connection'


@contextlib.contextmanager
def instance_lock(root):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'instance.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_state(root, state, instance, reason=None):
    """Bounded local transition history. No exceptions, tokens, URLs or contents."""
    events = clean_events(read_json(root / 'events.json').get('events', []))
    events.append(dict(time=time.time(), state=state, instance=instance,
                       **(issue(reason) if reason else {})))
    write_json(root / 'events.json', dict(events=events[-100:]))


class ConnectionReporter:
    """One writer, bounded in-memory history, best-effort persistence only.

    A locked/full/unavailable display or history file cannot cancel transport.
    Pending events are retried on the next heartbeat, up to the latest 100.
    """
    def __init__(self, root, instance):
        self.root, self.instance = root, instance
        self.state, self.reason = 'starting', None
        self.events = clean_events(read_json(root / 'events.json').get('events', []))
        self.dirty = False
        self.failed = {}
        self.last_issue = {}
        self.append()

    def append(self, details=None):
        self.events.append(dict(time=time.time(), state=self.state,
                                instance=self.instance, **(details or {})))
        self.events = self.events[-100:]
        self.dirty = True

    def diagnostic(self, reason, error=None):
        self.last_issue = issue(reason, error)
        self.reason = self.last_issue['reason']
        self.append(self.last_issue)
        self.flush()

    def update(self, next_state=None, *, reason=None, error=None):
        if next_state is not None:
            next_state = next_state if next_state in STATES else 'error'
            changed = self.state != next_state or self.reason != reason
            self.state, self.reason = next_state, reason
            if changed:
                details = issue(reason, error) if reason else {}
                if error is not None:
                    self.last_issue = details
                self.append(details)
        self.flush()

    def snapshot(self):
        return dict(schema=2, instance=self.instance, pid=os.getpid(), state=self.state,
                    updated=time.time(), reason=self.reason, last_issue=self.last_issue,
                    reporting_issues=list(self.failed.values()))

    def persist(self, name, value):
        try:
            write_json(self.root / (name + '.json'), value)
        except Exception as error:
            # Do not recursively log through the same failed sink or expose errors.
            details = issue(name.replace('events', 'history') + '_write_failed', error)
            if name not in self.failed:
                self.append(details)
            self.failed[name] = details
            return False
        if name in self.failed:
            del self.failed[name]
            self.append(issue(name.replace('events', 'history') + '_write_recovered'))
        return True

    def flush(self):
        self.persist('status', self.snapshot())
        if self.dirty:
            # Mark clean first so recovery events queued during persist remain pending.
            self.dirty = False
            if not self.persist('events', dict(events=self.events.copy())):
                self.dirty = True


def status(root=None):
    root = root or control_root()
    value = read_json(root / 'status.json')
    try:
        with instance_lock(root) as free:
            running = not free
    except OSError:
        return dict(state='unresponsive', running=None, reason='status_unavailable')
    updated = value.get('updated', 0)
    fresh = type(updated) in (float, int) and 0 <= time.time() - updated < 15
    details = dict(reason=clean_issue(value).get('reason'),
                   last_issue=clean_issue(value.get('last_issue')),
                   reporting_issues=[clean_issue(i) for i in value.get('reporting_issues', [])
                                     if clean_issue(i)] if isinstance(value.get('reporting_issues'), list) else [])
    if not running:
        # A locked status file must not hide a terminal cause saved to history.
        terminal = {'owner_stop', 'cancelled', 'required_file_missing', 'access_denied',
                    'configuration_invalid', 'unexpected_error', 'restart_limit'}
        for event in reversed(clean_events(read_json(root / 'events.json').get('events', []))):
            if (event['state'] in ('error', 'stopped') and event.get('reason') in terminal and
                    event['time'] >= (updated if type(updated) in (int, float) else 0)):
                value['state'] = event['state']
                details['reason'] = event['reason']
                details['last_issue'] = clean_issue(event)
                break
        if value.get('state') not in ('error', 'stopped'):
            details['reason'] = 'process_exit_unknown' if value else None
        return dict(state='error' if value.get('state') == 'error' else 'stopped', running=False, **details)
    if not fresh:
        return dict(state='unresponsive', running=True, **{**details, 'reason': 'status_unavailable'})
    return dict(state=value.get('state') if isinstance(value.get('state'), str) and value['state'] in STATES else 'unresponsive',
                instance=value.get('instance'), pid=value.get('pid'), schema=value.get('schema', 1),
                updated=updated, running=True, **details)


def diagnostics(root=None):
    root = root or control_root()
    events = clean_events(read_json(root / 'events.json').get('events', []))
    return dict(status=status(root), events=events,
                retention='latest 100 events; unsaved events can be lost if all storage is unavailable',
                recorded='fixed categories and numeric OS error codes only; no exception text or request contents')


async def supervise(workspace_root, root):
    from nexus.intake_connector import activate
    instance = uuid.uuid4().hex
    reporter = ConnectionReporter(root, instance)
    update = reporter.update
    update()
    task = None
    failures = 0
    stop_reason = 'cancelled'
    try:
        while True:
            started = time.monotonic()
            task = asyncio.create_task(activate(workspace_root, update, on_diagnostic=reporter.diagnostic))
            while not task.done():
                if read_json(root / 'stop.json').get('instance') == instance:
                    stop_reason = 'owner_stop'
                    return
                update()
                await asyncio.sleep(1)
            try:
                await task
                raise RuntimeError('service returned unexpectedly')
            except (ValueError, FileNotFoundError, PermissionError):
                # Invalid configuration/credentials need a real fix; no retry storm.
                raise
            except Exception as error:
                failures = 1 if time.monotonic() - started >= 60 else failures + 1
                if failures >= 3:
                    update('error', reason='restart_limit', error=error)
                    raise
                update('restarting', reason='unexpected_error', error=error)
                for _ in range(2 ** (failures - 1)):
                    if read_json(root / 'stop.json').get('instance') == instance:
                        stop_reason = 'owner_stop'
                        return
                    update()
                    await asyncio.sleep(1)
    except Exception as error:
        # No exception message: it could contain request data or credentials.
        reason = ('required_file_missing' if isinstance(error, FileNotFoundError) else
                  'access_denied' if isinstance(error, PermissionError) else
                  'configuration_invalid' if isinstance(error, ValueError) else 'unexpected_error')
        if reporter.state != 'error':
            update('error', reason=reason, error=error)
        return
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if reporter.state != 'error':
            update('stopped', reason=stop_reason)
        reporter.flush()


def run_host(workspace_root=None):
    root = control_root()
    # pythonw has no stdout/stderr; transport's fixed diagnostics go to NUL.
    with open(os.devnull, 'w') as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            with instance_lock(root) as acquired:
                if acquired:
                    asyncio.run(supervise(workspace_root or default_root(), root))


def start():
    current = status()
    if current['running'] is not False:
        return current
    previous_instance = read_json(control_root() / 'status.json').get('instance')
    subprocess.Popen([str(BASE / '.venv/Scripts/pythonw.exe'), '-m',
                      'nexus.workspace_service', 'run'], cwd=BASE,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(30):
        time.sleep(.2)
        value = status()
        if value['running'] and value.get('instance') != previous_instance and value['state'] not in ('unresponsive', 'stopped'):
            return value
    return status()


def stop():
    root = control_root()
    value = status(root)
    # Only address this running instance; never kill a PID or another process.
    instance = read_json(root / 'status.json').get('instance')
    if value['running'] and instance:
        write_json(root / 'stop.json', dict(instance=instance))
        for _ in range(50):
            time.sleep(.2)
            value = status(root)
            if not value['running']:
                break
    return value


def panel():
    import tkinter as tk
    app = tk.Tk(); app.title('NEXUS | 接続'); app.geometry('400x335')
    app.configure(bg='#141020'); app.resizable(False, False)
    tk.Label(app, text='N E X U S', fg='#cfb4ff', bg='#141020',
             font=('Segoe UI', 19)).pack(pady=(20, 8))
    label = tk.Label(app, fg='#f3edff', bg='#141020', font=('Yu Gothic UI', 12))
    label.pack(pady=8)
    names = dict(connected='● 接続 ON', reconnecting='◌ 再接続中',
                 circuit_open='◌ 通信待ち（5分ごとに再確認）', restarting='◌ 接続処理を再起動中',
                 starting='◌ 起動中', stopped='○ 接続 OFF', error='接続 OFF（エラー停止）',
                 unresponsive='状態を確認できません')
    detail = tk.Label(app, fg='#cfc2db', bg='#141020', wraplength=365,
                      font=('Yu Gothic UI', 10), height=3)
    detail.pack()
    def refresh():
        value = status()
        current = value['state']
        color = '#a9e0ce' if current == 'connected' else '#e5c78b' if current in ('reconnecting', 'starting', 'unresponsive') else '#b9aec7'
        label.config(text=names.get(current, '確認中'), fg=color)
        warning = value.get('reporting_issues') or []
        reason = value.get('reason') or (warning[0]['reason'] if warning else None)
        detail.config(text=REASONS.get(reason, ''))
        app.after(2000, refresh)
    row = tk.Frame(app, bg='#141020'); row.pack(pady=10)
    for text, action in [('接続する', start), ('停止する', stop)]:
        tk.Button(row, text=text, command=action, bg='#302341', fg='white',
                  relief='flat', width=12).pack(side='left', padx=6)
    def show_history():
        window = tk.Toplevel(app); window.title('NEXUS | 最近の接続記録'); window.geometry('650x430')
        output = tk.Text(window, wrap='word', font=('Yu Gothic UI', 10)); output.pack(fill='both', expand=True)
        for event in reversed(diagnostics()['events'][-30:]):
            at = time.strftime('%m/%d %H:%M:%S', time.localtime(event['time']))
            explanation = REASONS.get(event.get('reason'),
                                     '詳しい原因の記録なし' if event['state'] in ('error', 'stopped') else '状態の変化を記録')
            output.insert('end', f"{at}  {names.get(event['state'], event['state'])}\n{explanation}\n\n")
        output.config(state='disabled')
    tk.Button(app, text='最近の接続記録を見る', command=show_history, bg='#302341',
              fg='white', relief='flat').pack(pady=4)
    tk.Label(app, text='閉じても接続は続きます。PCの起動中のみ利用できます。',
             bg='#141020', fg='#b9aec7', font=('Yu Gothic UI', 9)).pack(pady=5)
    refresh(); app.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'start', 'stop', 'status', 'diagnostics', 'panel'])
    action = parser.parse_args().action
    if action == 'run': run_host()
    elif action == 'panel': panel()
    else: print(json.dumps(globals()[action]()))


if __name__ == '__main__': main()
