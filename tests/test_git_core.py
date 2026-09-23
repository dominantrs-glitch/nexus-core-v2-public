"""Synthetic native confirmations only. No credentials or real GitHub calls."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import InsufficientContext
from nexus.core_migration import prepare_core, freeze_prepared_core
from nexus.git_core import GitCore, digest, unpack_bundle
from nexus.owner import ApprovalDeclined
from nexus.task import Task


class SyntheticSurface:
    def __init__(self): self.requests, self.answer = [], True
    def confirm(self, request):
        self.requests.append(request)
        return self.answer


class MemoryCore:
    def __init__(self):
        self.document, self.receipts, self.writes, self.lose_reply = None, {}, 0, False
        self.unavailable, self.generation, self.state = False, 'test-1', 'active'

    def __call__(self, operation, args):
        if self.unavailable: raise ValueError('canonical unavailable')
        if operation == 'core_read':
            if self.document is None: raise ValueError('project unavailable')
            return dict(snapshot=str(self.writes),generation=self.generation,write_state=self.state,
                        document_sha256=digest(self.document),document=deepcopy(self.document))
        if args['expected_generation'] != self.generation: raise ValueError('generation changed')
        prior = self.receipts.get(args['request_id'])
        if prior:
            if prior[0] != digest(args): raise ValueError('different content')
            return prior[1]
        current = self.document['revision'] if self.document else 0
        if args['expected_revision'] != current: raise ValueError('revision conflict')
        if args['expected_document_sha256'] != (digest(self.document) if self.document else None): raise ValueError('checkpoint changed')
        self.document = dict(schema=1,project=args['project'],owner='owner',revision=current+1,
            previous_sha256=digest(self.document) if self.document else None,origin=deepcopy(args['origin']),files=deepcopy(args['files']))
        result = dict(project=args['project'],revision=current+1,document_sha256=digest(self.document))
        self.receipts[args['request_id']] = digest(args), result
        self.writes += 1
        if self.lose_reply:
            self.lose_reply = False
            raise ValueError('response lost after successful write')
        return result


class GitCoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.surface = SyntheticSurface()
        self.task = Task(self.root / 'source', 'example', 'owner', owner_root=self.root / 'owner', surface=self.surface)
        self.task.start(dict(goal='Synthetic test',acceptance={'file':['machine','contract','uat']},constraints=['Keep bytes'],source='synthetic fixture'))
        self.file = self.root / 'file.txt'
        self.file.write_text('original',encoding='utf-8')
        self.task.publish_output(self.file)
        self.package = self.root / 'package'
        prepare_core(self.task,self.package)
        freeze_prepared_core(self.task,self.package)
        self.remote = MemoryCore()
        self.store = GitCore(self.root / 'route','owner','test-1',owner_root=self.root / 'owner',surface=self.surface,call=self.remote)

    def test_frozen_import_and_disposable_native_update_preserve_confirmations(self):
        original = self.task.projects.export('example','owner')
        imported = self.store.import_prepared(self.package,'import')
        self.assertEqual(imported,self.store.import_prepared(self.package,'import'))
        self.assertEqual(self.remote.writes,1)
        with self.store.open('example') as session:
            self.assertEqual(session.task.snapshot(),original)
            session.task.verify('file','uat','pass','synthetic UAT',review_file=self.file)
            result = session.commit('verified')
            self.assertEqual(result['revision'],2)
            scratch = session.task.root
        self.assertFalse(scratch.exists())
        self.assertEqual(self.task.projects.export('example','owner'),original)
        with self.store.open('example') as session:
            audit = session.task.adapter.approvals.export_project('owner','example')['approvals']
            self.assertEqual([r['kind'] for r in audit],['confirmed_contract','user_uat'])
            self.assertEqual(session.task.snapshot()['events'][-1]['payload']['kind'],'uat')

    def test_declined_native_confirmation_does_not_change_canonical_or_create_receipt(self):
        self.store.import_prepared(self.package,'import')
        original = deepcopy(self.remote.document)
        self.surface.answer = False
        with self.store.open('example') as session:
            before = session.task.adapter.approvals.export_project('owner','example')
            with self.assertRaises(ApprovalDeclined):
                session.task.verify('file','uat','pass','synthetic declined',review_file=self.file)
            self.assertEqual(session.task.adapter.approvals.export_project('owner','example'),before)
        self.assertEqual(self.remote.document,original)

    def test_lost_reply_retry_does_not_repeat_native_confirmation(self):
        self.store.import_prepared(self.package,'import')
        with self.store.open('example') as session:
            session.task.verify('file','uat','pass','synthetic approved',review_file=self.file)
            self.remote.lose_reply = True
            with self.assertRaisesRegex(ValueError,'response lost'):
                session.commit('uat-once')
        count = len(self.surface.requests)
        result = self.store.retry('uat-once')
        self.assertEqual(result['revision'],2)
        self.assertEqual(len(self.surface.requests),count)
        self.assertEqual(self.remote.writes,2)

    def test_concurrent_working_copies_conflict_without_rebase_or_local_fallback(self):
        self.store.import_prepared(self.package,'import')
        with ExitStack() as stack:
            a,b = [stack.enter_context(self.store.open('example')) for _ in range(2)]
            a.task.verify('file','machine','pass','first')
            b.task.verify('file','machine','fail','second')
            a.commit('first')
            with self.assertRaisesRegex(ValueError,'revision conflict'): b.commit('second')
        with self.assertRaisesRegex(ValueError,'revision conflict'): self.store.retry('second')
        self.assertEqual(self.remote.document['revision'],2)
        self.remote.unavailable = True
        with self.assertRaisesRegex(ValueError,'unavailable'):
            with self.store.open('example'): pass

    def test_wrong_scope_changed_bytes_and_paths_refused_before_task_use(self):
        self.store.import_prepared(self.package,'import')
        with self.assertRaises(InsufficientContext):
            with self.store.open('wrong'): pass
        files = self.remote.document['files']
        with self.assertRaises(InsufficientContext):
            unpack_bundle({**files,'../escape':'YQ=='},self.root / 'invalid','owner','example')
        self.assertFalse((self.root / 'escape').exists())
        files['project.json'] = 'e30='
        with self.assertRaises(InsufficientContext):
            with self.store.open('example'): pass

    def test_unfrozen_package_not_importable_and_frozen_destination_not_writable(self):
        receipt = self.package / 'frozen.json'
        data = json.loads(receipt.read_text())
        receipt.write_text(json.dumps({**data,'status':'prepared'}))
        with self.assertRaises(InsufficientContext): self.store.import_prepared(self.package,'not-frozen')
        self.assertEqual(self.remote.writes,0)
        receipt.write_text(json.dumps(data))
        self.store.import_prepared(self.package,'import')
        self.remote.state = 'frozen'
        with self.store.open('example') as session:
            with self.assertRaisesRegex(InsufficientContext,'frozen'): session.commit('forbidden')


if __name__ == '__main__': unittest.main()
