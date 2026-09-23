"""Explicit foreground activation of an already approved text-only projection.

Do not run --activate until the owner approves destination and preview contents.
No account settings, credentials, autostart or relay deployments are modified.
"""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys

from nexus.connector import relay_app
from nexus.context_delivery import DeliveryGrant, build_context_server, project_delivery, select_delivery
from nexus.context_store import ContextStore
from nexus.task_entry import TaskEntry
from nexus.artifacts import InsufficientContext


class ReviewedTask:
    """Freeze the exact reviewed text, not just record IDs, for this connection."""
    def __init__(self, task, grant, preview):
        self.task, self.grant, self.preview = task, grant, preview
        self.project = task.project

    def read(self, operation):
        view = self.task.read(operation)
        if select_delivery(view, self.grant) != self.preview:
            raise InsufficientContext('reviewed preview changed')
        return view


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--grant', type=Path, required=True)
    parser.add_argument('--preview', type=Path, required=True)
    parser.add_argument('--origin')
    parser.add_argument('--relay-project')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    data = json.loads(args.grant.read_text(encoding='utf-8'))
    grant = DeliveryGrant(data['project'], data['contract_revision'], tuple(tuple(r) for r in data['records']))
    personal = Path(os.environ['LOCALAPPDATA']) / 'NexusCoreV2' / 'personal' / 'context.sqlite3'
    if not (args.root / 'projects.sqlite3').is_file() or not personal.is_file():
        parser.error('existing project and personal stores required')
    task = TaskEntry(args.root, grant.project, getpass.getuser(), personal_store=ContextStore(personal))
    preview = json.loads(args.preview.read_text(encoding='utf-8'))
    reviewed = ReviewedTask(task, grant, preview)
    value = project_delivery(reviewed, grant)
    if not args.activate:
        print(json.dumps({'status':'local-preview-verified', 'bytes':len(json.dumps(value,ensure_ascii=False).encode('utf-8')), 'network_started':False}))
        return
    if not args.origin or not args.relay_project:
        parser.error('approved origin and relay project required for activation')
    server = build_context_server(reviewed, grant)
    app = server.streamable_http_app(json_response=True, stateless_http=True, max_request_body_size=16384)
    asyncio.run(relay_app(args.origin, os.environ.get('NEXUS_CONNECTOR_TOKEN',''), args.relay_project, app))


if __name__ == '__main__': main()
