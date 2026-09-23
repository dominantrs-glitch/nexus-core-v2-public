"""Trusted local capture of scoped preferences; never an external authority API.

User statements are stored verbatim. Model interpretations stay candidates.
Neither is a confirmed Decision or permission to change a Contract.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
import uuid

from nexus.context_store import ContextStore
from nexus.owner import OwnerCapability, local_owner_data_root


HOME = 'personal-context'


class PersonalContext:
    def __init__(self, store, capability, owner):
        self.store, self.capability, self.owner = store, capability, owner

    def capture(self, identity, *, text, provenance, projects, operations,
                interpretation=False, expected_revision=0, status='current'):
        self.capability.authenticate(self.owner)
        if not isinstance(provenance, str) or not provenance.strip():
            raise ValueError('source location required')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('verbatim text required')
        if not isinstance(projects, frozenset) or not projects or '*' in projects:
            raise ValueError('explicit project scope required')
        if not isinstance(operations, frozenset) or not operations or '*' in operations:
            raise ValueError('explicit operation scope required')
        # Avoid silent conversion of a previous interpretation into owner evidence.
        prior = [r for r in self.store.export(HOME, self.owner)['contexts'] if r['id'] == identity]
        kind = 'candidate' if interpretation else 'personal'
        if prior and (prior[-1]['revision'] != expected_revision or prior[-1]['kind'] != kind):
            raise ValueError('revision or evidence-kind conflict')
        if not prior and expected_revision != 0:
            raise ValueError('revision conflict')
        source = self.store.register_source('capture-' + uuid.uuid4().hex, HOME, self.owner,
            json.dumps(dict(provenance=provenance, evidence_kind='interpretation' if interpretation else 'user-statement',
                            text=text), ensure_ascii=False))
        method = self.store.candidate if interpretation else self.store.save_personal_context
        return method(identity, HOME, self.owner, source=source, body=text,
            projects=projects | frozenset({HOME}), operations=operations,
            readers=frozenset({self.owner}), expected_revision=expected_revision, status=status)

    def read(self, project, operation, *, required=frozenset()):
        self.capability.authenticate(self.owner)
        result = self.store.resolve(project, self.owner, operation, required=required)
        return dict(project=project, operation=operation, items=[
            dict(id=i.record.id, revision=i.record.revision, text=i.record.body,
                 authority=i.record.authority, binding=i.binding,
                 source_id=i.record.source_id, source_revision=i.record.source_revision)
            for i in result.items], excluded=result.excluded)

    def export_local(self, destination):
        """Full owner archive, not a project handoff or external delivery payload."""
        self.capability.authenticate(self.owner)
        snapshot = self.store.export(HOME, self.owner)
        with Path(destination).open('x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        return {'local_archive': str(destination), 'contexts': len(snapshot['contexts'])}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Local scoped personal context; no network writes')
    parser.add_argument('--database', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'NexusCoreV2' / 'personal' / 'context.sqlite3')
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('identity')
    capture.add_argument('--file', type=Path, required=True, help='UTF-8 verbatim statement or interpretation')
    capture.add_argument('--source', required=True)
    capture.add_argument('--project', action='append', required=True)
    capture.add_argument('--operation', action='append', required=True)
    capture.add_argument('--interpretation', action='store_true')
    capture.add_argument('--expected-revision', type=int, default=0)
    capture.add_argument('--status', choices=['current', 'historical', 'suppressed', 'superseded'], default='current')
    read = sub.add_parser('read')
    read.add_argument('--project', required=True)
    read.add_argument('--operation', required=True)
    read.add_argument('--required', action='append', default=[])
    sub.add_parser('export-local').add_argument('destination', type=Path)
    args = parser.parse_args()
    owner = getpass.getuser()
    capability = OwnerCapability.open(local_owner_data_root() / 'owner.capability.json', owner)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    personal = PersonalContext(ContextStore(args.database), capability, owner)
    if args.command == 'capture':
        result = {'revision': personal.capture(args.identity, text=args.file.read_text(encoding='utf-8'),
            provenance=args.source, projects=frozenset(args.project), operations=frozenset(args.operation),
            interpretation=args.interpretation, expected_revision=args.expected_revision, status=args.status)}
    elif args.command == 'read':
        result = personal.read(args.project, args.operation, required=frozenset(args.required))
    else:
        result = personal.export_local(args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
