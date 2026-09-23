"""Opt-in native owner-local Git workspace. Never exposed through draft MCP.

SQLite is a fresh, disposable working copy of each pinned Git checkpoint. Native
Task/owner confirmation still performs changes. The entire validated result and
its retry receipt commit atomically to Git; failed calls never fall back locally.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import tempfile

from nexus.artifacts import InsufficientContext
from nexus.core_backup import export_core, restore_core
from nexus.core_freeze import archived
from nexus.core_migration import _hash, _verify_restore, _write_new, prepare_core, verify_package
from nexus.git_intake import GitIntake
from nexus.owner import ApprovalLedger, OwnerCapability, local_owner_data_root
from nexus.task import Task


PATH = re.compile(r'(?:manifest\.json|project\.json|context\.json|approvals\.json|artifacts/(?:manifest\.json|blobs/[a-f0-9]{64}))\Z')
MAX_BYTES = 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def pack_bundle(bundle, owner, project):
    bundle = Path(bundle)
    files = {}
    for path in sorted(bundle.rglob('*')):
        if path.is_symlink():
            raise InsufficientContext('native package link forbidden')
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix()
        if not PATH.fullmatch(relative) or path.stat().st_size > MAX_BYTES:
            raise InsufficientContext('native transport file unavailable or too large')
        files[relative] = base64.b64encode(path.read_bytes()).decode('ascii')
    if len(files) > 133 or len(json.dumps(files, separators=(',', ':')).encode()) > MAX_BYTES:
        raise InsufficientContext('native transport package too large; no truncation')
    _verify_restore(bundle, owner)
    state = json.loads((bundle / 'project.json').read_text(encoding='utf-8'))
    if state['project']['id'] != project or state['project']['owner'] != owner:
        raise InsufficientContext('native project identity mismatch')
    return files


def unpack_bundle(files, destination, owner, project):
    if not isinstance(files, dict) or len(files) > 133 or len(json.dumps(files).encode()) > MAX_BYTES + 1024:
        raise InsufficientContext('native transport package invalid')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for relative, encoded in files.items():
        if not isinstance(relative, str) or not PATH.fullmatch(relative) or not isinstance(encoded, str):
            raise InsufficientContext('native transport path invalid')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise InsufficientContext('native transport encoding invalid') from None
        if base64.b64encode(raw).decode('ascii') != encoded:
            raise InsufficientContext('native transport encoding invalid')
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if pack_bundle(destination, owner, project) != files:
        raise InsufficientContext('native transport package changed')


class GitCore:
    def __init__(self, root, owner, generation, *, owner_root=None, surface=None, call=None,
                 route_name=None, learning_store=None, learning_sources=None, personal_store=None):
        self.root, self.owner, self.generation = Path(root).resolve(), owner, generation
        self.owner_root = Path(owner_root) if owner_root else local_owner_data_root()
        self.surface = surface
        self._call = call or GitIntake(self.root)._invoke
        if route_name is not None and (not isinstance(route_name,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',route_name)):
            raise ValueError('bounded trusted route name required')
        if learning_store is not None and route_name is None:
            raise ValueError('stable registered source route required for learning')
        self.route_name, self.learning_store = route_name, learning_store
        self.personal_store = personal_store
        self.learning_sources = dict(learning_sources or {})
        if route_name is not None:
            if route_name in self.learning_sources:
                raise ValueError('source route already registered')
            self.learning_sources[route_name] = self

    def _authenticate(self):
        OwnerCapability.bootstrap(self.owner_root / 'owner.capability.json', self.owner).authenticate(self.owner)

    def _outbox(self, request_id):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError('bounded request id required')
        directory = self.root / 'native-outbox'
        directory.mkdir(parents=True, exist_ok=True)
        return directory / (digest([self.owner, self.generation, request_id]) + '.json')

    def _commit(self, args):
        self._authenticate()
        path = self._outbox(args['request_id'])
        if path.exists():
            if path.is_symlink() or json.loads(path.read_text(encoding='utf-8')) != args:
                raise ValueError('request id reused with different content')
        else:
            _write_new(path, args)
        # Persist exactly the retry payload BEFORE network I/O. Never re-run a
        # confirmation or callback after an ambiguous remote result.
        return self._call('core_write', args)

    def retry(self, request_id):
        self._authenticate()
        path = self._outbox(request_id)
        if path.is_symlink():
            raise InsufficientContext('native outbox link forbidden')
        args = json.loads(path.read_text(encoding='utf-8'))
        if args.get('request_id') != request_id or args.get('expected_generation') != self.generation:
            raise InsufficientContext('native retry identity mismatch')
        return self._call('core_write', args)

    def import_prepared(self, directory, request_id):
        self._authenticate()
        directory = Path(directory)
        review = verify_package(directory)
        path = directory / 'frozen.json'
        if path.is_symlink():
            raise InsufficientContext('frozen source receipt unavailable')
        frozen = json.loads(path.read_text(encoding='utf-8'))
        if (review['owner'] != self.owner or frozen.get('format') != 'nexus-core-frozen-package-v1'
                or frozen.get('status') != 'frozen' or frozen.get('project') != review['project']
                or frozen.get('source_digest') != review['source_digest']
                or frozen.get('package_sha256') != _hash(directory / 'package.json')
                or not str(frozen.get('generation', '')).startswith('native-freeze-')):
            raise InsufficientContext('matching frozen native source required')
        return self._commit(dict(project=review['project'], expected_revision=0, expected_document_sha256=None, expected_generation=self.generation,
            request_id=request_id, origin={key: frozen[key] for key in ('generation', 'source_digest', 'package_sha256')},
            files=pack_bundle(directory / 'bundle', self.owner, review['project'])))

    def prepare_import(self, task, directory):
        """Check transport eligibility BEFORE the separately authorized freeze."""
        self._authenticate()
        if task.owner != self.owner:
            raise InsufficientContext('native owner identity mismatch')
        review = prepare_core(task, directory)
        files = pack_bundle(Path(directory) / 'bundle', self.owner, task.project)
        return dict(review=review, transport_bytes=len(json.dumps(files, separators=(',', ':')).encode()),
                    status='review-only', source_frozen=archived(task.projects.database, task.project))

    @contextmanager
    def open(self, project):
        self._authenticate()
        view = self._call('core_read', dict(project=project))
        doc = view['document']
        if (view['generation'] != self.generation or doc['project'] != project or doc['owner'] != self.owner
                or view['document_sha256'] != digest(doc) or doc.get('schema') != 1
                or type(doc.get('revision')) is not int or doc['revision'] < 1
                or view['write_state'] not in ('active', 'frozen')):
            raise InsufficientContext('native canonical identity or checksum changed')
        with tempfile.TemporaryDirectory(prefix='nexus-native-working-') as temporary:
            base = Path(temporary)
            unpack_bundle(doc['files'], base / 'bundle', self.owner, project)
            restore_core(base / 'bundle', base / 'task', self.owner)
            task = Task(base / 'task', project, self.owner, owner_root=self.owner_root, surface=self.surface,
                        learning_store=self.learning_store, learning_sources=self.learning_sources, personal_store=self.personal_store)
            if self.route_name is not None:
                task.native_source = dict(route=self.route_name,generation=self.generation)
                task.native_checkpoint = dict(revision=doc['revision'],sha256=view['document_sha256'])
            # Use the restored audit for this transaction. Reusing the archived
            # shared approval ledger would block new receipts or create local-only ones.
            task.adapter.approvals = ApprovalLedger(base / 'task' / 'approvals.sqlite3')
            yield GitCoreSession(self, task, view, base)


class GitCoreSession:
    def __init__(self, store, task, view, base):
        self.store, self.task, self.view, self.base = store, task, view, base
        self.committed = False

    def commit(self, request_id):
        if self.committed:
            raise ValueError('open a fresh canonical snapshot for the next transaction')
        if self.view['write_state'] != 'active':
            raise InsufficientContext('native canonical project frozen')
        doc = self.view['document']
        # A session commits once; retry an uncertain outcome through store.retry.
        bundle = self.base / 'result'
        export_core(self.task.projects, self.task.contexts, self.task.artifacts, self.task.project, self.task.owner,
                    bundle, approvals=self.task.adapter.approvals)
        files = pack_bundle(bundle, self.task.owner, self.task.project)
        self.committed = True
        return self.store._commit(dict(project=self.task.project, expected_revision=doc['revision'],
            expected_document_sha256=self.view['document_sha256'],
            expected_generation=self.store.generation, request_id=request_id, origin=doc['origin'], files=files))
