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
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def record_state(root, state, instance):
    """Bounded local transition history. No exceptions, tokens, URLs or contents."""
    prior = read_json(root / 'events.json').get('events', [])
    events = prior if isinstance(prior, list) else []
    events = [e for e in events if isinstance(e, dict) and set(e) == {'time', 'state', 'instance'}]
    events.append(dict(time=time.time(), state=state, instance=instance))
    write_json(root / 'events.json', dict(events=events[-100:]))


def status(root=None):
    root = root or control_root()
    value = read_json(root / 'status.json')
    with instance_lock(root) as free:
        running = not free
    fresh = 0 <= time.time() - value.get('updated', 0) < 15
    if not running:
        return dict(state='error' if value.get('state') == 'error' else 'stopped', running=False)
    if not fresh:
        return dict(state='unresponsive', running=True)
    return dict(value, running=True)


async def supervise(workspace_root, root):
    from nexus.intake_connector import activate
    instance = uuid.uuid4().hex
    state = 'starting'
    def update(next_state=None):
        nonlocal state
        if next_state is not None:
            if state != next_state:
                record_state(root, next_state, instance)
            state = next_state
        write_json(root / 'status.json', dict(instance=instance, pid=os.getpid(),
                   state=state, updated=time.time()))
    update()
    record_state(root, state, instance)
    task = None
    failures = 0
    try:
        while True:
            started = time.monotonic()
            task = asyncio.create_task(activate(workspace_root, update))
            while not task.done():
                if read_json(root / 'stop.json').get('instance') == instance:
                    return
                update()
                await asyncio.sleep(1)
            try:
                await task
                raise RuntimeError('service returned unexpectedly')
            except (ValueError, FileNotFoundError, PermissionError):
                # Invalid configuration/credentials need a real fix; no retry storm.
                raise
            except Exception:
                failures = 1 if time.monotonic() - started >= 60 else failures + 1
                if failures >= 3:
                    raise
                update('restarting')
                for _ in range(2 ** (failures - 1)):
                    if read_json(root / 'stop.json').get('instance') == instance:
                        return
                    update()
                    await asyncio.sleep(1)
    except Exception:
        # No exception message: it could contain request data or credentials.
        update('error')
        return
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if state != 'error':
            update('stopped')


def run_host(workspace_root=None):
    root = control_root()
    # pythonw has no stdout/stderr; transport's fixed diagnostics go to NUL.
    with open(os.devnull, 'w') as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            with instance_lock(root) as acquired:
                if acquired:
                    asyncio.run(supervise(workspace_root or default_root(), root))


def start():
    if status()['running']:
        return status()
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
    app = tk.Tk(); app.title('NEXUS | 接続'); app.geometry('360x235')
    app.configure(bg='#141020'); app.resizable(False, False)
    tk.Label(app, text='N E X U S', fg='#cfb4ff', bg='#141020',
             font=('Segoe UI', 19)).pack(pady=(20, 8))
    label = tk.Label(app, fg='#f3edff', bg='#141020', font=('Yu Gothic UI', 12))
    label.pack(pady=8)
    names = dict(connected='● 接続 ON', reconnecting='◌ 再接続中',
                 circuit_open='◌ 通信待ち（5分ごとに再確認）', restarting='◌ 接続処理を再起動中',
                 starting='◌ 起動中', stopped='○ 接続 OFF', error='接続 OFF（起動失敗）',
                 unresponsive='状態を確認できません')
    def refresh():
        current = status()['state']
        color = '#a9e0ce' if current == 'connected' else '#e5c78b' if current in ('reconnecting', 'starting', 'unresponsive') else '#b9aec7'
        label.config(text=names.get(current, '確認中'), fg=color)
        app.after(2000, refresh)
    row = tk.Frame(app, bg='#141020'); row.pack(pady=10)
    for text, action in [('接続する', start), ('停止する', stop)]:
        tk.Button(row, text=text, command=action, bg='#302341', fg='white',
                  relief='flat', width=12).pack(side='left', padx=6)
    tk.Label(app, text='閉じても接続は続きます。PCの起動中のみ利用できます。',
             bg='#141020', fg='#b9aec7', font=('Yu Gothic UI', 9)).pack(pady=5)
    refresh(); app.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'start', 'stop', 'status', 'panel'])
    action = parser.parse_args().action
    if action == 'run': run_host()
    elif action == 'panel': panel()
    else: print(json.dumps(globals()[action]()))


if __name__ == '__main__': main()
