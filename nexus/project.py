"""Local trusted-owner Project ledger. Not exposed as a model-writable service.

Confirmation and UAT must originate from an authenticated owner adapter before
this API is exposed remotely. This ledger checks structure, not truth of reports.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from nexus.artifacts import Access, AccessDenied, InsufficientContext


KINDS = frozenset({'machine', 'contract', 'uat', 'operational'})
STATUSES = frozenset({'pass', 'fail', 'unavailable', 'not_verified', 'na'})


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('nonempty text required')
    return value


class ProjectStore:
    def __init__(self, database: Path, *, completion_check=None, context_store=None, artifact_store=None):
        self.database = database
        # Trusted application wiring, never model arguments. Default is fail-closed.
        self.completion_check = completion_check
        self.context_store = context_store
        self.artifact_store = artifact_store
        with self._db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                revision INTEGER NOT NULL, state TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS contracts(
                project TEXT NOT NULL REFERENCES projects(id), revision INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(project, revision));
              CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
                revision INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    def _project(self, db, project, actor):
        p = db.execute('SELECT * FROM projects WHERE id=?', (project,)).fetchone()
        if p is None or p['owner'] != actor:
            raise AccessDenied('project unavailable')
        return p

    def _event(self, db, project, revision, kind, payload):
        return db.execute('INSERT INTO events(project,revision,kind,payload) VALUES(?,?,?,?)',
                          (project, revision, kind, json.dumps(payload, ensure_ascii=False))).lastrowid

    @staticmethod
    def _validate_contract(goal, acceptance, constraints, source):
        for value in (goal, source):
            text(value)
        if not isinstance(acceptance, dict) or not acceptance:
            raise ValueError('acceptance required')
        for key, kinds in acceptance.items():
            text(key)
            if not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= KINDS:
                raise ValueError('explicit verification requirements required')
        if not isinstance(constraints, list):
            raise ValueError('constraints must be explicit list')
        for constraint in constraints:
            text(constraint)

    def confirm_contract(self, project, actor, *, goal, acceptance, constraints, source,
                         expected_revision=0, approval_receipt=None):
        """Trusted owner confirms an immutable revision; optimistic conflict check."""
        for value in (project, actor):
            text(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('invalid revision')
        self._validate_contract(goal, acceptance, constraints, source)
        if approval_receipt is not None:
            text(approval_receipt)
        payload = dict(goal=goal, acceptance=acceptance, constraints=constraints,
                       authority='confirmed', source=source)
        with self._db() as db:
            if expected_revision == 0:
                db.execute('INSERT INTO projects VALUES(?,?,0,?)', (project, actor, 'active'))
            p = self._project(db, project, actor)
            if p['revision'] != expected_revision:
                raise ValueError('revision conflict')
            revision = expected_revision + 1
            db.execute('INSERT INTO contracts VALUES(?,?,?)', (project, revision, json.dumps(payload, ensure_ascii=False)))
            db.execute("UPDATE projects SET revision=?,state='active' WHERE id=?", (revision, project))
            event = {'source': source}
            if approval_receipt is not None:
                event['approval_receipt'] = approval_receipt
            self._event(db, project, revision, 'confirmation', event)
            return revision

    def bind_output(self, project, actor, *, expected_revision, artifact_id, artifact_revision, sha256):
        """Bind identity verified by ArtifactStore; no locator or byte surrogate."""
        import re
        text(artifact_id)
        if type(artifact_revision) is not int or artifact_revision < 1 or re.fullmatch('[0-9a-f]{64}', sha256) is None:
            raise ValueError('invalid artifact identity')
        with self._db() as db:
            p = self._project(db, project, actor)
            if p['revision'] != expected_revision:
                raise ValueError('revision conflict')
            _, events, _ = self._current(db, p)
            prior = [e['payload']['revision'] for e in events if e['kind'] == 'output' and e['payload']['artifact_id'] == artifact_id]
            if prior and artifact_revision <= max(prior):
                raise ValueError('output revision must advance')
            self._event(db, project, p['revision'], 'output', dict(artifact_id=artifact_id, revision=artifact_revision, sha256=sha256))
            db.execute("UPDATE projects SET state='active' WHERE id=?", (project,))

    def _current(self, db, p):
        contract = json.loads(db.execute('SELECT payload FROM contracts WHERE project=? AND revision=?',
                                        (p['id'], p['revision'])).fetchone()[0])
        rows = db.execute('SELECT * FROM events WHERE project=? AND revision=? ORDER BY seq',
                          (p['id'], p['revision'])).fetchall()
        events = [dict(seq=r['seq'], kind=r['kind'], payload=json.loads(r['payload'])) for r in rows]
        output = next((e for e in reversed(events) if e['kind'] == 'output'), None)
        return contract, events, output

    def record_verification(self, project, actor, *, expected_revision, output_event,
                            acceptance, kind, status, source, approval_receipt=None):
        if kind not in KINDS or status not in STATUSES:
            raise ValueError('invalid verification')
        text(source)
        if approval_receipt is not None:
            text(approval_receipt)
        with self._db() as db:
            p = self._project(db, project, actor)
            c, events, output = self._current(db, p)
            if p['revision'] != expected_revision or output is None or output['seq'] != output_event:
                raise ValueError('stale verification target')
            if acceptance not in c['acceptance']:
                raise ValueError('unknown acceptance')
            payload = dict(output_event=output_event, acceptance=acceptance, kind=kind, status=status, source=source)
            if approval_receipt is not None:
                payload['approval_receipt'] = approval_receipt
            event = self._event(db, project, p['revision'], 'verification', payload)
            # A later report must force a fresh DONE gate, including fail after pass.
            db.execute("UPDATE projects SET state='active' WHERE id=?", (project,))
            return event

    def candidate(self, project, actor, *, evidence_events, body):
        """Learning stays non-binding and refers to existing evidence in this project."""
        text(body)
        if not isinstance(evidence_events, list) or not evidence_events or any(type(i) is not int for i in evidence_events):
            raise ValueError('evidence references required')
        with self._db() as db:
            p = self._project(db, project, actor)
            allowed = {r[0] for r in db.execute("SELECT seq FROM events WHERE project=? AND kind IN ('verification','correction')", (project,))}
            if not set(evidence_events) <= allowed:
                raise ValueError('evidence unavailable')
            return self._event(db, project, p['revision'], 'candidate', dict(
                evidence_events=sorted(set(evidence_events)), body=body, authority='candidate', binding=False))

    def inspection_html(self, project, actor):
        """Small escaped view of the canonical export, no separately edited state."""
        from html import escape
        snapshot = self.export(project, actor)
        return ('<!doctype html><html lang="ja"><meta charset="utf-8">'
                '<title>Nexus Project inspection</title><style>body{max-width:1000px;margin:40px auto;'
                'font:16px/1.7 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
                '<h1>Project inspection</h1><p>state: ' + escape(snapshot['project']['state']) +
                ' / current contract revision: ' + str(snapshot['project']['revision']) +
                '</p><p>過去版は履歴です。candidateは拘束力を持ちません。doneは記録された対象版の機械的ゲート結果です。</p><pre>' +
                escape(json.dumps(snapshot, ensure_ascii=False, indent=2)) + '</pre></html>')

    def correction(self, project, actor, *, expected_revision, output_event, acceptance, body, source):
        for value in (body, source):
            text(value)
        with self._db() as db:
            p = self._project(db, project, actor)
            c, events, output = self._current(db, p)
            if p['revision'] != expected_revision or output is None or output['seq'] != output_event or acceptance not in c['acceptance']:
                raise ValueError('stale correction target')
            event = self._event(db, project, p['revision'], 'correction', dict(
                output_event=output_event, acceptance=acceptance, body=body, source=source))
            db.execute("UPDATE projects SET state='rework' WHERE id=?", (project,))
            return event

    def finish(self, project, actor):
        """Only recorded exact-target PASSes qualify; missing/NA never imply PASS.

        Live required-context/original validation is mandatory in addition to
        reports. The trusted application supplies this validator at construction.
        """
        with self._db() as db:
            p = self._project(db, project, actor)
            c, events, output = self._current(db, p)
            trace = None
            latest = {}
            corrected = False
            if output:
                for e in events:
                    v = e['payload']
                    if e['kind'] == 'correction' and e['seq'] > output['seq']:
                        corrected = True
                    if e['kind'] == 'verification' and v['output_event'] == output['seq']:
                        latest[(v['acceptance'], v['kind'])] = v['status']
            missing = output is None or corrected or any(
                latest.get((a, k)) != 'pass' for a, kinds in c['acceptance'].items() for k in kinds)
            if not missing:
                try:
                    if self.completion_check is not None:
                        self.completion_check(project, p['revision'], output['payload'])
                    elif self.context_store is not None and self.artifact_store is not None:
                        self.context_store.validate_completion(project, actor, p['revision'], output['payload'], self.artifact_store)
                    else:
                        raise InsufficientContext('completion policy unavailable')
                except (InsufficientContext, AccessDenied) as error:
                    missing = True
                    # Owner-only ledger trace: deliberately generic so a failed
                    # scoped lookup never becomes an existence/content oracle.
                    trace = {'category': 'insufficient_context', 'detail': str(error)}
            state = 'blocked' if missing else 'done'
            # Closing always evaluates the learning evidence. Absence of a
            # correction is a valid result, never a reason to invent a lesson.
            corrections = [e['seq'] for e in events if e['kind'] == 'correction']
            assessment = dict(status='needs_verification' if missing else
                              'candidate_review_available' if corrections else 'no_correction_evidence',
                              correction_events=corrections,
                              candidate_events=[e['seq'] for e in events if e['kind'] == 'candidate'],
                              output_event=output['seq'] if output else None,
                              scope={'project': project, 'contract_revision': p['revision']},
                              authority='candidate', binding=False,
                              instruction='Review only evidence-backed lessons in this scope; no automatic owner-value or permission change.')
            if not any(e['kind'] == 'learning_evaluation' and e['payload'] == assessment for e in events):
                self._event(db, project, p['revision'], 'learning_evaluation', assessment)
            db.execute('UPDATE projects SET state=? WHERE id=?', (state, project))
            result = {'result': state}
            if trace is not None:
                result['trace'] = trace
            self._event(db, project, p['revision'], 'done_gate', result)
        if missing:
            raise InsufficientContext('required verification or revision unresolved')

    def export(self, project, actor):
        """Owner inspection/export from canonical ledger, including historical refs."""
        with self._db() as db:
            p = self._project(db, project, actor)
            contracts = [dict(revision=r['revision'], **json.loads(r['payload'])) for r in
                         db.execute('SELECT * FROM contracts WHERE project=? ORDER BY revision', (project,))]
            events = [dict(seq=r['seq'], revision=r['revision'], kind=r['kind'], payload=json.loads(r['payload'])) for r in
                      db.execute('SELECT * FROM events WHERE project=? ORDER BY seq', (project,))]
            return dict(schema='nexus.project.v1', project=dict(p), contracts=contracts, events=events)

    def restore_export(self, snapshot, actor, artifacts):
        """Restore a verified owner export into an empty ledger, never merge it."""
        if not isinstance(snapshot, dict) or snapshot.get('schema') != 'nexus.project.v1':
            raise ValueError('unsupported project export')
        project, contracts, events = snapshot.get('project'), snapshot.get('contracts'), snapshot.get('events')
        if not isinstance(project, dict) or set(project) != {'id', 'owner', 'revision', 'state'} or project['owner'] != actor:
            raise ValueError('invalid project export')
        if not isinstance(contracts, list) or not isinstance(events, list) or project['state'] not in {'active', 'rework', 'blocked', 'done'}:
            raise ValueError('invalid project export')
        if type(project['revision']) is not int or project['revision'] < 1 or [item.get('revision') for item in contracts] != list(range(1, project['revision'] + 1)):
            raise ValueError('invalid contract history')
        for contract in contracts:
            if set(contract) != {'revision', 'goal', 'acceptance', 'constraints', 'authority', 'source'} or contract['authority'] != 'confirmed':
                raise ValueError('invalid contract export')
            self._validate_contract(contract['goal'], contract['acceptance'], contract['constraints'], contract['source'])
        sequences = [item.get('seq') for item in events]
        # A project-scoped export can have gaps from another project's events.
        if any(type(seq) is not int or seq < 1 for seq in sequences) or sequences != sorted(set(sequences)):
            raise ValueError('invalid event history')
        for event in events:
            if set(event) != {'seq', 'revision', 'kind', 'payload'} or event['revision'] < 1 or event['revision'] > project['revision'] or not isinstance(event['payload'], dict):
                raise ValueError('invalid event export')
            if event['kind'] == 'output':
                payload = event['payload']
                if set(payload) != {'artifact_id', 'revision', 'sha256'}:
                    raise ValueError('invalid output export')
                manifest = artifacts.describe(payload['artifact_id'], payload['revision'], Access(actor, project['id']))
                if manifest['sha256'] != payload['sha256']:
                    raise InsufficientContext('restored output identity mismatch')
        with self._db() as db:
            if db.execute('SELECT 1 FROM projects LIMIT 1').fetchone():
                raise FileExistsError('project store is not empty')
            db.execute('INSERT INTO projects VALUES(?,?,?,?)', (project['id'], actor, project['revision'], project['state']))
            for contract in contracts:
                payload = {key: value for key, value in contract.items() if key != 'revision'}
                db.execute('INSERT INTO contracts VALUES(?,?,?)', (project['id'], contract['revision'], json.dumps(payload, ensure_ascii=False)))
            for event in events:
                db.execute('INSERT INTO events(seq,project,revision,kind,payload) VALUES(?,?,?,?,?)',
                    (event['seq'], project['id'], event['revision'], event['kind'], json.dumps(event['payload'], ensure_ascii=False)))
