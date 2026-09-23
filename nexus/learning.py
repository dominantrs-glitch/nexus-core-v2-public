"""Local, scoped reuse of repaired-work evidence. Never a permission/approval API.

The agent extracts meaning and implements changes. Core checks identity, evidence,
currentness and delivery scope; a measured candidate never becomes a global rule.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext, LocalBinaryProvider
from nexus.context_store import ContextStore, SourceRef
from nexus.project import text

HOME = 'work-learning'


def work_types(values):
    if not isinstance(values, (list, tuple)) or any(
            not isinstance(v, str) or not v.strip() or v != v.strip() or len(v) > 64 or v == '*'
            for v in values):
        raise ValueError('explicit work type names required')
    return sorted(set(values))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def checked_output(snapshot):
    """Machine and Contract reports only. No inference of UAT/operational success."""
    revision = snapshot['project']['revision']
    events = [e for e in snapshot['events'] if e['revision'] == revision]
    output = next((e for e in reversed(events) if e['kind'] == 'output'), None)
    if output is None or any(e['kind'] == 'correction' and e['seq'] > output['seq'] for e in events):
        raise InsufficientContext('repaired output required')
    latest = {}
    for e in events:
        p = e['payload']
        if e['kind'] == 'verification' and p['output_event'] == output['seq']:
            latest[(p['acceptance'], p['kind'])] = e
    checks = []
    for criterion in snapshot['contracts'][-1]['acceptance']:
        for kind in ('machine', 'contract'):
            e = latest.get((criterion, kind))
            if e is None or e['payload']['status'] != 'pass':
                raise InsufficientContext('machine and contract verification required')
            checks.append(e)
    return output, checks


class Learning:
    def __init__(self, store, capability, owner, *, native_sources=None):
        self.store, self.capability, self.owner = store, capability, owner
        # Trusted application wiring, never a filesystem path or URL from a lesson.
        self.native_sources = dict(native_sources or {})

    @staticmethod
    def _locations(task, proof):
        if getattr(task, 'native_source', None) is not None:
            return dict(schema='nexus.lesson-proof.v2', native_source=dict(task.native_source))
        return dict(schema='nexus.lesson-proof.v1', database=str(task.projects.database.resolve()),
                    context_database=str(task.contexts.database.resolve()),
                    artifact_database=str(task.artifacts.database.resolve()),
                    blob=str((task.artifacts.provider.root / proof['output']['payload']['sha256']).resolve()))

    @classmethod
    def _located_proof(cls, task, proof):
        result = {k:v for k,v in proof.items() if k not in
                  ('schema','database','context_database','artifact_database','blob','native_source','native_checkpoint')}
        result.update(cls._locations(task, proof))
        if result['schema'] == 'nexus.lesson-proof.v2':
            result['native_checkpoint'] = dict(task.native_checkpoint)
        return result

    def capture(self, task, identity, *, principle, rationale, projects=frozenset(), required_constraints=(),
                forbidden_constraints=(), expected_revision=0, reuse_scope='project',
                applicable_work_types=(), reuse_basis=''):
        self.capability.authenticate(self.owner)
        if task.owner != self.owner:
            raise AccessDenied('learning unavailable')
        for value in (identity, principle, rationale):
            text(value)
        if not isinstance(projects, frozenset) or '*' in projects or any(not isinstance(p, str) or not p.strip() for p in projects):
            raise ValueError('explicit destination projects required')
        kinds = work_types(applicable_work_types)
        if reuse_scope not in ('project', 'task_type', 'general'):
            raise ValueError('unknown reuse scope')
        if reuse_scope == 'project':
            if not projects or kinds:
                raise ValueError('project scope requires destinations, not work types')
        else:
            text(reuse_basis)  # attributed local reuse authorization/rationale, not learned permission
            if projects or (reuse_scope == 'task_type' and not kinds) or (reuse_scope == 'general' and kinds):
                raise ValueError('scope and work type mismatch')
        for values in (required_constraints, forbidden_constraints):
            if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError('explicit condition lists required')
        if (reuse_scope == 'project' and not required_constraints) or set(required_constraints) & set(forbidden_constraints):
            raise ValueError('nonconflicting applicability conditions required')
        task.read('review')  # includes required context and original gates
        snapshot = task.snapshot()
        output, checks = checked_output(snapshot)
        corrections = [e for e in snapshot['events'] if e['revision'] == snapshot['project']['revision']
                       and e['kind'] == 'correction' and e['seq'] < output['seq']]
        if not corrections:
            raise InsufficientContext('source correction required')
        p = output['payload']
        with task.artifacts.open_verified(p['artifact_id'], p['revision'], Access(self.owner, task.project)):
            pass
        # Owner-only proof. Raw correction text and artifact bytes are not delivered to B.
        proof = dict(project=task.project, contract_revision=snapshot['project']['revision'],
                     contract_hash=digest(snapshot['contracts'][-1]), output=output,
                     checks=checks, correction=corrections[-1])
        proof = self._located_proof(task, proof)
        if proof['schema'] == 'nexus.lesson-proof.v2':
            # Uncommitted scratch work must not become durable reusable evidence.
            self._validate_proof(proof)
        body = encoded(dict(schema='nexus.lesson.v1', principle=principle, rationale=rationale,
                            reuse_scope=reuse_scope, applicable_work_types=kinds, reuse_basis=reuse_basis,
                            judgment_layer='candidate' if reuse_scope == 'general' else 'work_knowledge',
                            required_constraints=list(required_constraints),
                            forbidden_constraints=list(forbidden_constraints),
                            evidence_level='repaired-case-machine-and-contract', binding=False))
        old = [r for r in self.store.export(HOME, self.owner)['contexts'] if r['id'] == identity]
        if old:
            prior = old[-1]
            if (prior['revision'] == expected_revision + 1 and prior['body'] == body
                    and set(prior['projects']) == set(projects | {HOME}) and prior['status'] == 'current'):
                with self.store._db() as db:
                    source = db.execute('SELECT body FROM sources WHERE id=? AND revision=?',
                                        (prior['source_id'], prior['source_revision'])).fetchone()
                if source and source['body'] == encoded(proof):
                    return prior['revision']
            if prior['revision'] != expected_revision:
                raise ValueError('revision conflict')
        elif expected_revision != 0:
            raise ValueError('revision conflict')
        source_id = f'lesson:{identity}:r{expected_revision + 1}'
        proof_text = encoded(proof)
        # Resume a crash between immutable source write and context write.
        with self.store._db() as db:
            source = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if source:
            if source['body'] != proof_text or source['owner'] != self.owner or source['project'] != HOME:
                raise ValueError('source conflict')
            ref = SourceRef(source_id, source['revision'], source['sha256'])
        else:
            ref = self.store.register_source(source_id, HOME, self.owner, proof_text)
        return self.store.candidate(identity, HOME, self.owner, source=ref, body=body,
            projects=projects | {HOME}, operations=frozenset({'implement', 'review'}),
            readers=frozenset({self.owner}), expected_revision=expected_revision)

    def _proof(self, record):
        with self.store._db() as db:
            row = db.execute('SELECT * FROM sources WHERE id=? AND revision=?',
                             (record.source_id, record.source_revision)).fetchone()
            latest = db.execute('SELECT MAX(revision) FROM sources WHERE id=?', (record.source_id,)).fetchone()[0]
        if (row is None or latest != record.source_revision or row['owner'] != self.owner
                or hashlib.sha256(row['body'].encode('utf-8')).hexdigest() != row['sha256']):
            raise InsufficientContext('lesson evidence unavailable')
        proof = json.loads(row['body'])
        if proof['schema'] not in ('nexus.lesson-proof.v1','nexus.lesson-proof.v2'):
            raise InsufficientContext('unknown evidence schema')
        return proof

    def _valid_source(self, record):
        return self._validate_proof(self._proof(record))

    def _validate_proof(self, proof, *, historical_archive=False):
        if proof.get('schema') == 'nexus.lesson-proof.v2':
            return self._validate_native_proof(proof, historical_archive=historical_archive)
        path = Path(proof['database'])
        from nexus.core_freeze import archived
        if archived(path, proof['project']) and not historical_archive:
            raise InsufficientContext('lesson source archived; canonical source rebind required')
        # Read-only: a missing source must not silently create an empty database.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            project = db.execute('SELECT * FROM projects WHERE id=? AND owner=?', (proof['project'], self.owner)).fetchone()
            if project is None or project['revision'] != proof['contract_revision']:
                raise InsufficientContext('lesson evidence changed')
            contract = json.loads(db.execute('SELECT payload FROM contracts WHERE project=? AND revision=?',
                                             (proof['project'], project['revision'])).fetchone()[0])
            contract['revision'] = project['revision']
            events = [dict(r) for r in db.execute('SELECT seq,revision,kind,payload FROM events WHERE project=? ORDER BY seq', (proof['project'],))]
            for event in events:
                event['payload'] = json.loads(event['payload'])
        snapshot = dict(project=dict(project), contracts=[contract], events=events)
        output, checks = checked_output(snapshot)
        if (digest(contract) != proof['contract_hash'] or output != proof['output']
                or checks != proof['checks'] or proof['correction'] not in events):
            raise InsufficientContext('lesson evidence changed')
        with Path(proof['blob']).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != output['payload']['sha256']:
                raise InsufficientContext('lesson original changed')
        context_path, artifact_path = Path(proof['context_database']), Path(proof['artifact_database'])
        if not context_path.is_file() or not artifact_path.is_file():
            raise InsufficientContext('lesson source gates unavailable')
        contexts = ContextStore(context_path)
        artifacts = ArtifactStore(artifact_path, LocalBinaryProvider(Path(proof['blob']).parent))
        contexts.validate_completion(proof['project'], self.owner, proof['contract_revision'],
                                     output['payload'], artifacts, operation='review')
        return proof

    def _validate_native_proof(self, proof, *, historical_archive=False):
        source, checkpoint = proof.get('native_source'), proof.get('native_checkpoint')
        if (not isinstance(source, dict) or set(source) != {'route','generation'}
                or not isinstance(source['route'],str) or not isinstance(source['generation'],str)
                or not isinstance(checkpoint,dict) or set(checkpoint) != {'revision','sha256'}
                or type(checkpoint['revision']) is not int or checkpoint['revision'] < 1
                or not isinstance(checkpoint['sha256'],str) or len(checkpoint['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in checkpoint['sha256'])):
            raise InsufficientContext('native lesson source unavailable')
        store = self.native_sources.get(source['route'])
        if store is None or store.owner != self.owner or store.generation != source['generation']:
            raise InsufficientContext('native lesson route unavailable or changed')
        with store.open(proof['project']) as session:
            current = session.view
            if (current['write_state'] != 'active' and not historical_archive
                    or current['document']['revision'] < checkpoint['revision']
                    or current['document']['revision'] == checkpoint['revision'] and current['document_sha256'] != checkpoint['sha256']):
                raise InsufficientContext('native lesson checkpoint changed or archived')
            # Reuse the exact native Contract/event/original/ACL checks. These paths
            # exist only inside this scope and are never persisted as the proof.
            local = dict(proof, schema='nexus.lesson-proof.v1', database=str(session.task.projects.database.resolve()),
                context_database=str(session.task.contexts.database.resolve()), artifact_database=str(session.task.artifacts.database.resolve()),
                blob=str((session.task.artifacts.provider.root / proof['output']['payload']['sha256']).resolve()))
            self._validate_proof(local)
        return proof

    def rebind_source(self, task, identity, expected_revision):
        """Explicit owner-local recovery after restoring the source Core project.

        Only storage locations change. Evidence, candidate body, scope and history
        stay intact. A new candidate revision invalidates old application traces.
        This cannot establish that an offline backup is the latest canonical;
        the operator must verify that recovery checkpoint before invoking it.
        """
        self.capability.authenticate(self.owner)
        if task.owner != self.owner:
            raise AccessDenied('learning unavailable')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('explicit existing revision required')
        with self.store._db() as db:
            record = next((r for r in self.store._records(db, HOME, self.owner) if r.id == identity), None)
        if (record is None or record.status != 'current' or record.authority != 'candidate'
                or self.owner not in record.readers):
            raise InsufficientContext('current candidate unavailable')
        if record.revision not in (expected_revision, expected_revision + 1):
            raise ValueError('revision conflict')
        proof = self._proof(record)
        if proof['project'] != task.project:
            raise InsufficientContext('source project mismatch')
        locations = self._locations(task, proof)
        if record.revision == expected_revision + 1:
            # Recover an uncertain successful rebind, not an unrelated later edit.
            prior = proof.get('relocated_from', {})
            if prior.get('candidate_revision') != expected_revision or any(proof.get(k) != v for k,v in locations.items()):
                raise ValueError('revision conflict')
            self._validate_proof(proof)
            return record.revision
        # An available original must still be valid; do not resurrect a revoked or
        # regressed source by selecting an older restored copy.
        if proof['schema'] == 'nexus.lesson-proof.v2' or Path(proof['database']).exists():
            # An archive may establish historical equality during explicit rebind,
            # never current eligibility. Still check every event, byte and scope;
            # the destination below must be live and independently valid.
            self._validate_proof(proof, historical_archive=True)
        rebound = self._located_proof(task, proof)
        rebound['relocated_from'] = dict(source_id=record.source_id, source_revision=record.source_revision,
                                        source_sha256=digest(proof), candidate_revision=record.revision)
        self._validate_proof(rebound)  # byte hash, exact events/Contract, current ACL/context/original gates
        if all(proof.get(k) == v for k,v in locations.items()):
            return record.revision
        source_id = f'lesson:{identity}:rebind:r{expected_revision + 1}'
        body = encoded(rebound)
        with self.store._db() as db:
            existing = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        if existing:
            if existing['owner'] != self.owner or existing['project'] != HOME or existing['body'] != body:
                raise ValueError('source conflict')
            source = SourceRef(existing['id'], existing['revision'], existing['sha256'])
        else:
            source = self.store.register_source(source_id, HOME, self.owner, body)
        return self.store.candidate(identity, HOME, self.owner, source=source, body=record.body,
            projects=record.projects, operations=record.operations, readers=record.readers,
            expected_revision=expected_revision)

    def read(self, project, operation, contract, *, mode='delegate', task_work_types=()):
        self.capability.authenticate(self.owner)
        kinds = set(work_types(task_work_types))
        if mode not in ('delegate', 'independent', 'red-team'):
            raise ValueError('unknown judgment mode')
        if mode != 'delegate':
            return dict(items=[], excluded=0, mode=mode, binding=False)
        # Local owner-only registry. Record ACL/status/operation is checked before
        # classification; this does not grant a remote reader cross-project access.
        selected = self.store.resolve(HOME, self.owner, operation, required=frozenset())
        items, excluded = [], selected.excluded
        for item in selected.items:
            try:
                record = item.record
                data = json.loads(record.body)
                if data['schema'] != 'nexus.lesson.v1' or record.authority != 'candidate':
                    raise ValueError('not a lesson')
                scope = data.get('reuse_scope', 'project')  # old lessons never become broad
                if scope == 'project':
                    if project not in record.projects:
                        raise ValueError('outside project scope')
                elif scope == 'task_type':
                    if not kinds.intersection(work_types(data['applicable_work_types'])) or not data.get('reuse_basis'):
                        raise ValueError('outside work type scope')
                elif scope == 'general':
                    if not data.get('reuse_basis'):
                        raise ValueError('reuse basis required')
                else:
                    raise ValueError('unknown scope')
                constraints = set(contract['constraints'])
                if (not set(data['required_constraints']) <= constraints
                        or set(data['forbidden_constraints']) & constraints):
                    raise ValueError('not applicable')
                self._valid_source(record)
                items.append(dict(id=record.id, revision=record.revision, authority='candidate',
                                  binding=False, **{k: v for k, v in data.items() if k != 'binding'},
                                  source_id=record.source_id, source_revision=record.source_revision))
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, InsufficientContext, AccessDenied):
                excluded += 1
        return dict(items=items, excluded=excluded, mode=mode, binding=False)

    def suppress(self, identity, expected_revision):
        self.capability.authenticate(self.owner)
        records = [r for r in self.store.export(HOME, self.owner)['contexts'] if r['id'] == identity]
        if not records or records[-1]['revision'] != expected_revision:
            raise ValueError('revision conflict')
        r = records[-1]
        with self.store._db() as db:
            s = db.execute('SELECT * FROM sources WHERE id=? AND revision=?', (r['source_id'], r['source_revision'])).fetchone()
        return self.store.candidate(identity, HOME, self.owner,
            source=SourceRef(s['id'], s['revision'], s['sha256']), body=r['body'],
            projects=frozenset(r['projects']), operations=frozenset(r['operations']),
            readers=frozenset(r['readers']), expected_revision=expected_revision, status='suppressed')
