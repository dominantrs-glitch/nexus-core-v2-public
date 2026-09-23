"""Native Task implementation; CLI selects an explicitly activated canonical route."""
import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext, LocalBinaryProvider
from nexus.context_store import ContextStore
from nexus.owner import open_local_owner_adapter
from nexus.personal import PersonalContext
from nexus.project import ProjectStore
from nexus.learning import Learning, checked_output, digest, work_types


class Task:
    def __init__(self, root, project, owner, *, owner_root=None, surface=None, personal_store=None, learning_store=None, learning_sources=None):
        self.root, self.project, self.owner = Path(root), project, owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.contexts = ContextStore(self.root / 'context.sqlite3')
        restored = self.root / 'artifacts' / 'manifest.sqlite3'
        if restored.is_file():
            self.artifacts = ArtifactStore(restored, LocalBinaryProvider(self.root / 'artifacts' / 'blobs'))
        else:
            self.artifacts = ArtifactStore(self.root / 'artifacts.sqlite3', LocalBinaryProvider(self.root / 'blobs'))
        self.projects = ProjectStore(self.root / 'projects.sqlite3', context_store=self.contexts, artifact_store=self.artifacts)
        self.adapter = open_local_owner_adapter(owner, self.projects, self.contexts, data_root=owner_root, surface=surface)
        self.personal = PersonalContext(personal_store, self.adapter.capability, owner) if personal_store else None
        self.learning = Learning(learning_store, self.adapter.capability, owner, native_sources=learning_sources) if learning_store else None

    def snapshot(self):
        self.adapter.capability.authenticate(self.owner)
        from nexus.core_freeze import archived
        if archived(self.projects.database, self.project):
            raise InsufficientContext('native project archived for migration; use the configured canonical destination')
        return self.projects.export(self.project, self.owner)

    def start(self, draft):
        if set(draft) != {'goal', 'acceptance', 'constraints', 'source'}:
            raise ValueError('goal, acceptance, constraints and source required')
        with self.projects._db() as db:
            exists = db.execute('SELECT 1 FROM projects WHERE id=?', (self.project,)).fetchone()
        if not exists:
            self.adapter.confirm_contract(self.project, self.owner, **draft)
        snapshot = self.snapshot()
        contract = snapshot['contracts'][-1]
        if any(contract[k] != v for k, v in draft.items()):
            raise ValueError('existing contract differs; explicit contract revision required')
        revision = snapshot['project']['revision']
        identity = f'{self.project}:contract-reference:{revision}'
        records = self.contexts.export(self.project, self.owner)
        if not any(c['id'] == identity for c in records['contexts']):
            body = json.dumps(contract, ensure_ascii=False, sort_keys=True)
            sources = [s for s in records['sources'] if s['id'] == identity]
            if sources:
                from nexus.context_store import SourceRef
                s = sources[-1]
                if s['body'] != body:
                    raise ValueError('contract reference conflict')
                source = SourceRef(s['id'], s['revision'], s['sha256'])
            else:
                source = self.contexts.register_source(identity, self.project, self.owner, body)
            self.contexts.derive(identity, self.project, self.owner, source=source, body=body,
                projects=frozenset({self.project}), operations=frozenset({'implement', 'review', 'finish'}), readers=frozenset({self.owner}))
        for operation in ('implement', 'review', 'finish'):
            try:
                self.contexts.policy(self.project, self.owner, revision, operation)
            except InsufficientContext:
                self.contexts.set_operation_policy(self.project, self.owner, contract_revision=revision,
                    operation=operation, principal=self.owner, required_context=frozenset({identity}),
                    required_authority={identity: 'derived'}, required_artifacts=())
        return self.read('implement')

    def read(self, operation, *, mode='delegate'):
        snapshot = self.snapshot()
        try:
            return self._read(operation, snapshot, mode=mode)
        except (InsufficientContext, AccessDenied):
            with self.projects._db() as db:
                self.projects._event(db, self.project, snapshot['project']['revision'], 'context_gate',
                    {'operation': operation, 'result': 'insufficient_context'})
                db.execute("UPDATE projects SET state='blocked' WHERE id=?", (self.project,))
            raise InsufficientContext('required operation context or original unavailable') from None

    def _read(self, operation, snapshot, *, mode='delegate'):
        if mode not in ('delegate', 'independent', 'red-team'):
            raise ValueError('unknown judgment mode')
        revision = snapshot['project']['revision']
        policy = self.contexts.policy(self.project, self.owner, revision, operation)
        selected = self.contexts.resolve(self.project, self.owner, operation,
            required=policy.required_context, required_authority=policy.required_authority)
        for identity, version in policy.required_artifacts:
            with self.artifacts.open_verified(identity, version, Access(self.owner, self.project)):
                pass
        current = [e for e in snapshot['events'] if e['revision'] == revision]
        classification = next((e for e in reversed(current) if e['kind'] == 'work_classification'), None)
        kinds = classification['payload']['work_types'] if classification else []
        output = next((e for e in reversed(current) if e['kind'] == 'output'), None)
        return dict(project=snapshot['project'], contract=snapshot['contracts'][-1],
            work_classification=classification,
            context=[dict(id=i.record.id, revision=i.record.revision, body=i.record.body,
                          authority=i.record.authority, binding=i.binding) for i in selected.items],
            personal=self.personal.read(self.project, operation) if self.personal and mode == 'delegate' else {'items': [], 'available': False},
            learning=self.learning.read(self.project, operation, snapshot['contracts'][-1], mode=mode, task_work_types=kinds)
                     if self.learning else {'items': [], 'available': False},
            output=output,
            pending_correction=next((e for e in reversed(current) if e['kind'] == 'correction'
                                     and (output is None or e['seq'] > output['seq'])), None))

    def classify_work(self, kinds, source, *, expected_event=0):
        """Local agent classification; independent of Contract/permission/owner values."""
        from nexus.project import text
        text(source)
        kinds = work_types(kinds)
        self.adapter.capability.authenticate(self.owner)
        payload = dict(work_types=kinds, source=source, authority='derived', binding=False)
        with self.projects._db() as db:
            p = self.projects._project(db, self.project, self.owner)
            _, events, _ = self.projects._current(db, p)
            old = next((e for e in reversed(events) if e['kind'] == 'work_classification'), None)
            if old and old['payload'] == payload:
                return old['seq']
            if (old['seq'] if old else 0) != expected_event:
                raise ValueError('classification conflict')
            return self.projects._event(db, self.project, p['revision'], 'work_classification', payload)

    def learn(self, identity, **options):
        if self.learning is None:
            raise InsufficientContext('learning store unavailable')
        return {'id': identity, 'revision': self.learning.capture(self, identity, **options),
                'authority': 'candidate', 'binding': False}

    def apply_learning(self, identity, revision, reason):
        """Record intent before agent work, not execute code or grant permission.

        The local agent must independently check Contract and existing permissions.
        Exact constraints only filter relevance; they cannot prove semantic safety.
        """
        from nexus.project import text
        text(reason)
        view = self.read('implement')
        lesson = next((i for i in view['learning']['items'] if i['id'] == identity and i['revision'] == revision), None)
        if lesson is None:
            raise InsufficientContext('applicable lesson unavailable')
        payload = dict(lesson=lesson, reason=reason, contract_hash=digest(view['contract']),
                       classification=view['work_classification'],
                       baseline=view['output'], authority='candidate', binding=False)
        with self.projects._db() as db:
            p = self.projects._project(db, self.project, self.owner)
            contract, events, output = self.projects._current(db, p)
            contract = dict(contract, revision=p['revision'])
            if p['revision'] != view['project']['revision'] or digest(contract) != payload['contract_hash']:
                raise ValueError('revision conflict')
            if (output or {}).get('seq') != (view['output'] or {}).get('seq'):
                raise ValueError('output conflict')
            classification = next((e for e in reversed(events) if e['kind'] == 'work_classification'), None)
            if (classification or {}).get('seq') != (view['work_classification'] or {}).get('seq'):
                raise ValueError('classification conflict')
            for e in events:
                if e['kind'] == 'learning_application' and e['payload'] == payload:
                    return e['seq']
            return self.projects._event(db, self.project, p['revision'], 'learning_application', payload)

    def evaluate_learning(self, application):
        """Bind outcome to exact output/checks. Never counts as UAT or DONE."""
        self.read('review')
        with self.projects._db() as db:
            p = self.projects._project(db, self.project, self.owner)
            contract, events, output = self.projects._current(db, p)
            contract = dict(contract, revision=p['revision'])
            app = next((e for e in events if e['seq'] == application and e['kind'] == 'learning_application'), None)
            if app is None or output is None or output['seq'] <= application or digest(contract) != app['payload']['contract_hash']:
                raise InsufficientContext('new output for current application required')
            # Ensure the original still exists, not merely a PASS stored against metadata.
            out = output['payload']
            with self.artifacts.open_verified(out['artifact_id'], out['revision'], Access(self.owner, self.project)):
                pass
            lesson = app['payload']['lesson']
            classification = next((e for e in reversed(events) if e['kind'] == 'work_classification'), None)
            if (classification or {}).get('seq') != (app['payload'].get('classification') or {}).get('seq'):
                raise InsufficientContext('work classification changed; re-evaluate application')
            kinds = classification['payload']['work_types'] if classification else []
            current = self.learning.read(self.project, 'review', contract, task_work_types=kinds)['items'] if self.learning else []
            if not any(i == lesson for i in current):
                raise InsufficientContext('lesson withdrawn or evidence changed')
            snapshot = dict(project=dict(p), contracts=[contract], events=[dict(e, revision=p['revision']) for e in events])
            try:
                _, checks = checked_output(snapshot)
                result = 'pass'
            except InsufficientContext:
                latest = {}
                for e in events:
                    v = e['payload']
                    if e['kind'] == 'verification' and v['output_event'] == output['seq']:
                        latest[(v['acceptance'], v['kind'])] = e
                checks = list(latest.values())
                result = 'fail' if any(e['payload']['status'] == 'fail' for e in checks) else 'not_verified'
            payload = dict(application=application, lesson_id=lesson['id'], lesson_revision=lesson['revision'],
                           output=output, checks=checks, result=result, uat='not_inferred', operational='not_inferred')
            for e in events:
                if e['kind'] == 'learning_outcome' and e['payload'] == payload:
                    return payload
            self.projects._event(db, self.project, p['revision'], 'learning_outcome', payload)
            return payload

    def publish_output(self, path, *, mime='text/plain'):
        view = self.read('implement')
        identity = self.project + ':output'
        with self.artifacts._connect() as db:
            previous = db.execute('SELECT COALESCE(MAX(revision),0) FROM revisions WHERE id=?', (identity,)).fetchone()[0]
        with Path(path).open('rb') as stream:
            revision = self.artifacts.register_stream(identity, self.project, stream, mime, (self.owner,), expected_revision=previous)
        manifest = self.artifacts.describe(identity, revision, Access(self.owner, self.project))
        self.adapter.bind_output(self.project, self.owner, expected_revision=view['project']['revision'],
            artifact_id=identity, artifact_revision=revision, sha256=manifest['sha256'])
        return self.read('review')

    def verify(self, acceptance, kind, status, source, *, review_file=None):
        view = self.read('review')
        output = view['output']
        if output is None:
            raise InsufficientContext('output required')
        payload = output['payload']
        with self.artifacts.open_verified(payload['artifact_id'], payload['revision'], Access(self.owner, self.project)):
            pass
        if kind == 'uat':
            if review_file is None:
                raise InsufficientContext('visible review file required')
            with Path(review_file).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != payload['sha256']:
                raise InsufficientContext('visible review file must match output')
        return self.adapter.record_verification(self.project, self.owner,
            expected_revision=view['project']['revision'], output_event=output['seq'],
            acceptance=acceptance, kind=kind, status=status, source=source)

    def correct(self, acceptance, text, source):
        view = self.read('review')
        if view['output'] is None:
            raise InsufficientContext('output required')
        event = self.projects.correction(self.project, self.owner,
            expected_revision=view['project']['revision'], output_event=view['output']['seq'],
            acceptance=acceptance, body=text, source=source)
        return self.projects.candidate(self.project, self.owner, evidence_events=[event], body=text)

    def finish(self):
        self.read('finish')
        self.projects.finish(self.project, self.owner)
        return self.snapshot()


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Nexus local project operator')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--request-id', help='Stable retry identity for a configured Git mutation')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('start').add_argument('draft', type=Path)
    read = sub.add_parser('read')
    read.add_argument('--operation', choices=['implement', 'review', 'finish'], default='implement')
    read.add_argument('--mode', choices=['delegate', 'independent', 'red-team'], default='delegate')
    sub.add_parser('learn').add_argument('json_file', type=Path)
    sub.add_parser('classify-work').add_argument('json_file', type=Path)
    apply = sub.add_parser('apply-learning')
    apply.add_argument('identity')
    apply.add_argument('--revision', type=int, required=True)
    apply.add_argument('--reason', required=True)
    sub.add_parser('evaluate-learning').add_argument('application', type=int)
    suppress = sub.add_parser('suppress-learning')
    suppress.add_argument('identity')
    suppress.add_argument('--revision', type=int, required=True)
    rebind = sub.add_parser('rebind-learning-source')
    rebind.add_argument('identity')
    rebind.add_argument('--revision', type=int, required=True)
    output = sub.add_parser('output')
    output.add_argument('file', type=Path)
    output.add_argument('--mime', default='text/plain')
    verify = sub.add_parser('verify')
    verify.add_argument('--acceptance', required=True)
    verify.add_argument('--kind', choices=['machine', 'contract', 'uat', 'operational'], required=True)
    verify.add_argument('--status', choices=['pass', 'fail', 'unavailable', 'not_verified', 'na'], required=True)
    verify.add_argument('--source', required=True)
    verify.add_argument('--review-file', type=Path)
    correct = sub.add_parser('correct')
    correct.add_argument('--acceptance', required=True)
    correct.add_argument('--file', type=Path, required=True)
    correct.add_argument('--source', required=True)
    sub.add_parser('finish')
    sub.add_parser('inspect')
    sub.add_parser('retry').add_argument('request_id')
    args = parser.parse_args()
    personal_db = Path(os.environ['LOCALAPPDATA']) / 'NexusCoreV2' / 'personal' / 'context.sqlite3'
    personal_db.parent.mkdir(parents=True, exist_ok=True)
    learning_db = personal_db.parent / 'learning.sqlite3'
    from nexus.task_entry import TaskEntry
    import base64
    task = TaskEntry(args.root, args.project, getpass.getuser(), personal_store=ContextStore(personal_db),
                     learning_store=ContextStore(learning_db))
    if args.command == 'read': result = task.read(args.operation, mode=args.mode)
    elif args.command == 'inspect': result = task.snapshot()
    elif args.command == 'retry': result = task.retry(args.request_id)
    else:
        if args.command == 'start': payload = json.loads(args.draft.read_text(encoding='utf-8'))
        elif args.command in ('learn', 'classify-work'): payload = json.loads(args.json_file.read_text(encoding='utf-8'))
        elif args.command == 'apply-learning': payload = dict(identity=args.identity, revision=args.revision, reason=args.reason)
        elif args.command == 'evaluate-learning': payload = dict(application=args.application)
        elif args.command in ('suppress-learning', 'rebind-learning-source'):
            payload = dict(identity=args.identity, expected_revision=args.revision)
        elif args.command == 'output': payload = dict(data=base64.b64encode(args.file.read_bytes()).decode('ascii'), mime=args.mime)
        elif args.command == 'verify':
            payload = dict(acceptance=args.acceptance, kind=args.kind, status=args.status, source=args.source)
            if args.review_file is not None:
                from nexus.core_migration import _hash
                payload.update(review_file=str(args.review_file.resolve()), review_sha256=_hash(args.review_file))
        elif args.command == 'correct': payload = dict(acceptance=args.acceptance, text=args.file.read_text(encoding='utf-8'), source=args.source)
        else: payload = {}
        result = task.run(args.command, payload, request_id=args.request_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
