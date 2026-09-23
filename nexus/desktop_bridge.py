"""Local Desktop adapter. JSON on stdin; no shell, web listener or remote tool."""
import json
import os
from pathlib import Path
import sys

from nexus.intake import Intake, default_root
from nexus.git_intake import open_intake
from nexus import workspace_service


def dispatch(request, workspace=None):
    if not isinstance(request, dict):
        raise ValueError('object required')
    action = request.get('action')
    if action == 'status': return workspace_service.status()
    if action == 'start': return workspace_service.start()
    if action == 'stop': return workspace_service.stop()
    workspace = workspace or open_intake(default_root())
    if action == 'list':
        return workspace.list(request.get('query', ''), offset=request.get('offset', 0),snapshot=request.get('snapshot'))
    project = request.get('project')
    if action in ('read', 'open'):
        page = workspace.read(project)
        if action == 'open':
            folder = request.get('folder')
            if folder not in ('project', '05_output'):
                raise ValueError('unsupported folder')
            base = workspace.folder(project)
            target = base if folder == 'project' else base / folder
            os.startfile(str(target))
            return dict(opened=True)
        notes = list(page['notes'])
        snapshot = page.get('snapshot')
        revision = page['revision']
        while page['next_offset'] is not None:
            page = workspace.read(project, offset=page['next_offset'], snapshot=snapshot)
            if page['revision'] != revision: raise ValueError('project changed during read; reread')
            notes.extend(page['notes'])
        return dict(title=page['title'], revision=page['revision'],
                    notes=notes, overview=page['overview'], state='consultation-not-completion')
    raise ValueError('unsupported action')


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raw = sys.stdin.read(16385)
        if len(raw) > 16384: raise ValueError('input too large')
        result = dispatch(json.loads(raw))
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(json.dumps(dict(error='local-operation-failed')))
        raise SystemExit(1)


if __name__ == '__main__': main()
