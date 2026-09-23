"""Ordinary native entry with explicit per-project cutover and durable transactions.

Only a trusted local activation installs a route. A failed configured route never
falls back to an archive. Local personal/learning stores remain owner-local.
"""
import base64
import json
from pathlib import Path
import tempfile

from nexus.artifacts import InsufficientContext
from nexus.core_freeze import verify_frozen_core
from nexus.core_migration import _write_new, verify_package, _hash
from nexus.git_core import GitCore, digest
from nexus.owner import ApprovalLedger


PRIVATE = {'learn', 'suppress-learning', 'rebind-learning-source'}
WRITES = PRIVATE | {'start', 'classify-work', 'apply-learning', 'evaluate-learning', 'output', 'verify', 'correct', 'finish'}


def _read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 3 * 1024 * 1024:
        raise InsufficientContext('native entry record unavailable')
    return json.loads(path.read_text(encoding='utf-8'))


def route_path(root, project, owner):
    return Path(root).resolve() / '.nexus-native-routes' / (digest([project, owner]) + '.json')


def _observe(core, project, view, *, current=True):
    """Keep local monotonic evidence; a branch rollback is not fresh current data."""
    directory = core.root / 'native-observed' / digest([core.owner, core.generation, project])
    if directory.is_symlink():
        raise InsufficientContext('native checkpoint history link forbidden')
    directory.mkdir(parents=True, exist_ok=True)
    revision = view['document']['revision']
    names = list(directory.iterdir())
    if any(not p.name.endswith('.json') or len(p.stem) != 20 or not p.stem.isdecimal() for p in names):
        raise InsufficientContext('native checkpoint history invalid')
    if current and names and revision < max(int(p.stem) for p in names):
        raise InsufficientContext('native canonical checkpoint rolled back')
    path = directory / f'{revision:020d}.json'
    value = dict(revision=revision, sha256=view['document_sha256'])
    if not path.exists():
        try:
            _write_new(path, value)
        except FileExistsError:
            pass  # Concurrent identical readers are harmless; compare below.
    if _read(path) != value:
        raise InsufficientContext('native canonical checkpoint replaced')


def activate(task, core, package):
    """Activate only an already frozen source and matching imported destination."""
    task.adapter.capability.authenticate(task.owner)
    core._authenticate()
    if task.owner != core.owner or core.route_name is None:
        raise InsufficientContext('matching registered native owner route required')
    review = verify_package(package)
    frozen = _read(Path(package) / 'frozen.json')
    if (review['project'] != task.project or review['owner'] != task.owner
            or frozen.get('format') != 'nexus-core-frozen-package-v1' or frozen.get('status') != 'frozen'
            or frozen.get('project') != task.project or frozen.get('source_digest') != review['source_digest']
            or frozen.get('package_sha256') != _hash(Path(package) / 'package.json')):
        raise InsufficientContext('matching frozen migration package required')
    origin = {key: frozen[key] for key in ('generation', 'source_digest', 'package_sha256')}
    verify_frozen_core(task, origin)
    config = _read(core.root / 'canonical-git.json')
    if config.get('native_owner') != task.owner or config.get('generation') != core.generation:
        raise InsufficientContext('native destination configuration mismatch')
    with core.open(task.project) as session:
        if session.view['write_state'] != 'active' or session.view['document']['origin'] != origin:
            raise InsufficientContext('active destination with matching source required')
        session.task.read('implement')
        _observe(core, task.project, session.view)
    value = dict(schema=1, project=task.project, owner=task.owner, source_root=str(task.root.resolve()),
        approvals=str(Path(task.adapter.approvals.database).resolve()), owner_root=str(core.owner_root.resolve()),
        route_root=str(core.root), route_name=core.route_name, generation=core.generation,
        config_sha256=digest(config), origin=origin)
    path = route_path(task.root, task.project, task.owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read(path) != value:
            raise InsufficientContext('native route already configured differently')
    else:
        _write_new(path, value)
    return dict(project=task.project, status='canonical-git-active', generation=core.generation)


class TaskEntry:
    def __init__(self, root, project, owner, *, owner_root=None, surface=None, personal_store=None, learning_store=None, call=None):
        self.root, self.project, self.owner = Path(root).resolve(), project, owner
        self.options = dict(owner_root=owner_root, surface=surface, personal_store=personal_store, learning_store=learning_store)
        self.call = call

    def _resolve(self):
        from nexus.task import Task
        path = route_path(self.root, self.project, self.owner)
        if not path.exists():
            if path.is_symlink():
                raise InsufficientContext('native route link unavailable')
            return Task(self.root, self.project, self.owner, **self.options), None
        route = _read(path)
        if (set(route) != {'schema','project','owner','source_root','approvals','owner_root','route_root',
                          'route_name','generation','config_sha256','origin'} or route['schema'] != 1
                or (route['project'],route['owner'],route['source_root']) != (self.project,self.owner,str(self.root))):
            raise InsufficientContext('native route identity mismatch')
        config = _read(Path(route['route_root']) / 'canonical-git.json')
        if digest(config) != route['config_sha256']:
            raise InsufficientContext('native route configuration changed; review required')
        options = {**self.options, 'owner_root': Path(route['owner_root'])}
        source = Task(self.root, self.project, self.owner, **options)
        source.adapter.approvals = ApprovalLedger(route['approvals'])
        verify_frozen_core(source, route['origin'])
        core = GitCore(route['route_root'], self.owner, route['generation'], route_name=route['route_name'],
                       call=self.call, **options)
        return core, route

    def _check(self, session, route):
        if session.view['write_state'] != 'active' or session.view['document']['origin'] != route['origin']:
            raise InsufficientContext('canonical destination frozen or source changed')
        _observe(session.store, self.project, session.view)

    def read(self, operation, *, mode='delegate'):
        target, route = self._resolve()
        if route is None:
            return target.read(operation, mode=mode)
        with target.open(self.project) as session:
            self._check(session, route)
            return session.task.read(operation, mode=mode)

    def snapshot(self):
        target, route = self._resolve()
        if route is None:
            return target.snapshot()
        with target.open(self.project) as session:
            self._check(session, route)
            return session.task.snapshot()

    def run(self, command, payload, *, request_id=None):
        if command not in WRITES:
            raise ValueError('unknown native command')
        target, route = self._resolve()
        if route is None:
            return dispatch(target, command, payload)
        if not request_id:
            raise ValueError('request id required for canonical mutation')
        outbox = target._outbox(request_id)
        intent = outbox.with_suffix('.intent.json')
        result = outbox.with_suffix('.result.json')
        desired = dict(project=self.project, command=command, payload=payload)
        # Exclusive intent prevents two processes executing the same confirmation.
        if intent.exists():
            if _read(intent) != desired:
                raise ValueError('request id reused with different command')
            return self._retry(target, route, request_id, intent, result)
        if outbox.exists() or result.exists():
            raise InsufficientContext('native request already reserved')
        with target.open(self.project) as session:
            self._check(session, route)
            _write_new(intent, desired)
            value = dispatch(session.task, command, payload)
            _write_new(result, value)
            if command in PRIVATE:
                return dict(result=value, storage='owner-local-learning', binding=False)
            receipt = session.commit(request_id)
            _observe(target, self.project, dict(document={'revision': receipt['revision']}, document_sha256=receipt['document_sha256']), current=False)
        return dict(result=value, storage='canonical-git', receipt=receipt)

    def _retry(self, core, route, request_id, intent, result):
        saved = _read(intent)
        if saved.get('project') != self.project or saved.get('command') not in WRITES:
            raise InsufficientContext('native transaction identity mismatch')
        # No callback replay if interrupted before the durable outbox. The source
        # remains frozen; review the saved attempt before preparing a fresh one.
        if not result.exists():
            raise InsufficientContext('native attempt interrupted before result; inspect before a new request')
        value = _read(result)
        if saved['command'] in PRIVATE:
            with core.open(self.project) as session:
                self._check(session, route)
            return dict(result=value, storage='owner-local-learning', binding=False)
        if not core._outbox(request_id).exists():
            raise InsufficientContext('native attempt interrupted before outbox; inspect before a new request')
        with core.open(self.project) as session:
            self._check(session, route)
        receipt = core.retry(request_id)
        # The receipt can be older than the currently read head. It is historical
        # success, not a new current snapshot; never move observation backwards.
        return dict(result=value, storage='canonical-git', receipt=receipt)

    def retry(self, request_id):
        target, route = self._resolve()
        if route is None:
            raise ValueError('canonical native route required')
        outbox = target._outbox(request_id)
        return self._retry(target, route, request_id, outbox.with_suffix('.intent.json'), outbox.with_suffix('.result.json'))


def dispatch(task, command, payload):
    """Payload contains captured text/bytes; file inputs cannot change on retry."""
    if command == 'start': return task.start(payload)
    if command == 'classify-work': return task.classify_work(**payload)
    if command == 'apply-learning': return task.apply_learning(**payload)
    if command == 'evaluate-learning': return task.evaluate_learning(**payload)
    if command == 'learn':
        options = dict(payload, projects=frozenset(payload.get('projects', [])))
        return task.learn(**options)
    if command == 'suppress-learning': return task.learning.suppress(**payload)
    if command == 'rebind-learning-source':
        return dict(id=payload['identity'], revision=task.learning.rebind_source(task, **payload), binding=False)
    if command == 'output':
        data = base64.b64decode(payload['data'], validate=True)
        with tempfile.TemporaryDirectory(prefix='nexus-output-') as temporary:
            path = Path(temporary) / 'output'
            path.write_bytes(data)
            return task.publish_output(path, mime=payload['mime'])
    if command == 'verify':
        options = dict(payload)
        checksum = options.pop('review_sha256', None)
        if checksum is not None and _hash(Path(options['review_file'])) != checksum:
            raise InsufficientContext('visible review file changed')
        return task.verify(**options)
    if command == 'correct': return task.correct(**payload)
    if command == 'finish': return task.finish()
    raise ValueError('unknown native command')
