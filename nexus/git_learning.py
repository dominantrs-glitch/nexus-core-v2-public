"""Stage a new scoped native candidate from current Git evidence, not a private DB copy.

The caller commits the native session explicitly. Draft model tools cannot publish
this record. Sharing/withdrawal changes the candidate's native version and status.
"""
import json
import re

from nexus.artifacts import InsufficientContext
from nexus.context_store import ContextStore, SourceRef
from nexus.git_core import digest
from nexus.learning import HOME, Learning, encoded


def stage_shared_candidate(session, identity, *, principle, rationale, destinations,
                           required_constraints=(), forbidden_constraints=(), expected_revision=0,
                           reuse_scope='project', applicable_work_types=(), reuse_basis=''):
    task = session.task
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**53-1:
        raise ValueError('explicit candidate revision required')
    if (not isinstance(identity,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',identity)
            or not isinstance(destinations,frozenset) or not destinations
            or any(not isinstance(p,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',p) for p in destinations)):
        raise ValueError('explicit bounded candidate and destination identities required')
    if session.view['write_state'] != 'active' or not getattr(task,'native_source',None):
        raise InsufficientContext('active registered native Git source required')
    # Use the existing repair/candidate validator in disposable storage. No private
    # personal/learning database is read, copied, modified or sent by this operation.
    scratch = ContextStore(session.base / ('candidate-' + identity + '.sqlite3'))
    learning = Learning(scratch,task.adapter.capability,task.owner,native_sources=session.store.learning_sources)
    learning.capture(task,identity,principle=principle,rationale=rationale,
        projects=destinations if reuse_scope=='project' else frozenset(),required_constraints=required_constraints,
        forbidden_constraints=forbidden_constraints,reuse_scope=reuse_scope,
        applicable_work_types=applicable_work_types,reuse_basis=reuse_basis)
    snapshot = scratch.export(HOME,task.owner)
    candidate = snapshot['contexts'][-1]
    proof = json.loads(snapshot['sources'][-1]['body'])
    data = json.loads(candidate['body'])
    data.update(schema='nexus.shared-lesson.v1',destinations=sorted(destinations))
    source = dict(schema='nexus.shared-lesson-proof.v1',project=task.project,generation=session.store.generation,
        checkpoint=proof['native_checkpoint'],contract_revision=proof['contract_revision'],
        contract_sha256=digest(task.snapshot()['contracts'][-1]),output_event=proof['output']['seq'],
        output_sha256=proof['output']['payload']['sha256'],correction_event=proof['correction']['seq'],
        check_events=sorted(e['seq'] for e in proof['checks']))
    native_id = 'shared-lesson:' + identity
    source_id = f'{native_id}:proof:r{expected_revision + 1}'
    body = encoded(source)
    with task.contexts._db() as db:
        prior = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
    if prior:
        if prior['project'] != task.project or prior['owner'] != task.owner or prior['body'] != body:
            raise ValueError('shared evidence source conflict')
        reference = SourceRef(prior['id'],prior['revision'],prior['sha256'])
    else:
        reference = task.contexts.register_source(source_id,task.project,task.owner,body)
    version = task.contexts.candidate(native_id,task.project,task.owner,source=reference,body=encoded(data),
        projects=destinations|{task.project},operations=frozenset({'implement','review'}),
        readers=frozenset({task.owner}),expected_revision=expected_revision)
    return dict(id=native_id,revision=version,authority='candidate',binding=False,status='staged-not-committed')


def withdraw_shared_candidate(session, identity, expected_revision):
    task = session.task
    task.adapter.capability.authenticate(task.owner)
    records = [r for r in task.contexts.export(task.project,task.owner)['contexts'] if r['id']==identity]
    if not records or records[-1]['revision'] != expected_revision or records[-1]['kind'] != 'candidate':
        raise ValueError('current shared candidate revision required')
    record = records[-1]
    if json.loads(record['body']).get('schema') != 'nexus.shared-lesson.v1':
        raise ValueError('shared candidate required')
    with task.contexts._db() as db:
        source = db.execute('SELECT * FROM sources WHERE id=? AND revision=?',
                            (record['source_id'],record['source_revision'])).fetchone()
    return task.contexts.candidate(identity,task.project,task.owner,
        source=SourceRef(source['id'],source['revision'],source['sha256']),body=record['body'],
        projects=frozenset(record['projects']),operations=frozenset(record['operations']),
        readers=frozenset(record['readers']),expected_revision=expected_revision,status='suppressed')
