"""Durable pre-contract project intake shared by local agents and scoped MCP.

All externally supplied statements are attributed claims, not owner receipts.
The database is canonical; folders are storage/inspection, never executable input.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
from nexus.intake_freeze import install as install_fences, writable
from nexus.source_documents import source_documents


KINDS = {'goal', 'acceptance', 'constraint', 'explicit_choice', 'preference',
         'research', 'proposal', 'question', 'correction', 'source'}
FOLDERS = ('01_raw', '02_web', '03_context', '04_work', '05_output')
OVERVIEW_HEADER = '【画面用の概要】\n'
OVERVIEW_GUIDANCE = ('After a meaningful batch of project saves, write one short plain-Japanese display overview using save_project_note: '
    'kind=proposal, evidence=model_inference, quote="", body starts with 【画面用の概要】 followed by a newline. '
    'Explain purpose, important limits and next step in everyday words; explain unavoidable jargon. '
    'Use only observed source notes, label historical/unknown status, never invent completion or approval. '
    'source identifies the project revision and consulted notes. Use expected_revision from the latest receipt; '
    'supersede the previous overview note if present. Do not summarize full conversations. '
    'An overview is an AI explanation, never a Contract, Decision or permission.')


def default_root():
    return Path(os.environ['LOCALAPPDATA']) / 'NexusCoreV2' / 'intake'


def text(value, limit=8000):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
        raise ValueError('nonempty bounded UTF-8 text required')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', value):
        raise ValueError('invalid identifier')
    if value.split('.')[0].lower() in {'con','prn','aux','nul', *('com'+str(i) for i in range(1,10)), *('lpt'+str(i) for i in range(1,10))}:
        raise ValueError('reserved identifier')
    return value


class Intake:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, revision INTEGER NOT NULL,
                  remote INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes (
                  id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
                  revision INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                  source TEXT NOT NULL, evidence TEXT NOT NULL, quote TEXT NOT NULL,
                  supersedes TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS requests (
                  key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
            ''')
            install_fences(db)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'intake.sqlite3', timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def folder(self, project):
        base = self.root / 'projects' / identifier(project)
        # Reject symlink/junction escape, including intermediate existing parents.
        if not base.resolve().is_relative_to(self.root):
            raise ValueError('storage boundary')
        for name in FOLDERS:
            child = base / name
            if not child.resolve().is_relative_to(base.resolve()):
                raise ValueError('storage boundary')
            child.mkdir(parents=True, exist_ok=True)
        return base

    def _project(self, db, project, remote):
        identifier(project)
        row = db.execute('SELECT * FROM projects WHERE id=?', (project,)).fetchone()
        if row is None or (remote and not row['remote']):
            raise ValueError('project unavailable')
        return dict(row)

    def _retry(self, db, key, payload):
        text(key, 150)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = db.execute('SELECT * FROM requests WHERE key=?', (key,)).fetchone()
        if previous:
            if previous['digest'] != digest:
                raise ValueError('request key reused with different content')
            return digest, json.loads(previous['result'])
        return digest, None

    def _receipt(self, db, key, digest, result):
        db.execute('INSERT INTO requests VALUES (?,?,?)', (key,digest,json.dumps(result)))
        return result

    def _kind(self, db, project, note_id):
        """A correction inherits its original category, including old stored rows."""
        seen=set()
        while note_id:
            if note_id in seen:raise ValueError('invalid correction chain')
            seen.add(note_id)
            note=db.execute('SELECT kind,supersedes FROM notes WHERE id=? AND project=?',(note_id,project)).fetchone()
            if note is None:raise ValueError('correction target unavailable')
            if note['kind']!='correction' or note['supersedes'] is None:return note['kind']
            note_id=note['supersedes']
        raise ValueError('correction target unavailable')

    def create(self, title, source, request_id, *, remote=False):
        text(title, 300); text(source, 2000)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            digest, prior = self._retry(db, request_id, ['create',title,source,remote])
            if prior:
                self._project(db, prior['project'], remote)
                writable(db, prior['project'])
                self.folder(prior['project'])
                return prior
            project = 'p-' + uuid.uuid4().hex[:16]
            # This is a new local identity, before registration. Routed creates
            # already chose their destination and must not consult cloud here.
            Intake.folder(self,project)
            db.execute('INSERT INTO projects VALUES (?,?,0,?,?)', (project,title,int(remote),source))
            return self._receipt(db,request_id,digest,dict(project=project,revision=0,status='saved-draft',binding=False))

    def list(self, query='', *, remote=False, offset=0, snapshot=None):
        if snapshot is not None: raise ValueError('snapshot is only supported by the Git route')
        if not isinstance(query,str) or len(query)>200 or type(offset) is not int or offset<0:
            raise ValueError('invalid query')
        query=query.strip()
        with self.db() as db:
            total=db.execute('SELECT COUNT(*) FROM projects WHERE (?=0 OR remote=1) AND instr(lower(title),lower(?))>0',
                             (int(remote),query)).fetchone()[0]
            rows = db.execute('SELECT id,title,revision FROM projects WHERE (?=0 OR remote=1) AND instr(lower(title),lower(?))>0 ORDER BY id LIMIT 26 OFFSET ?', (int(remote),query,offset)).fetchall()
        return dict(projects=[dict(r) for r in rows[:25]], next_offset=offset+25 if len(rows)>25 else None,
                    search=dict(status='not_searched' if not query else 'matches' if total else 'no_match',
                                scope='project_titles',total_matches=total))

    def save(self, project, kind, body, source, evidence, quote, expected_revision, request_id, *, supersedes=None, remote=False):
        if kind not in KINDS or evidence not in {'user_statement','model_inference','external_source'}:
            raise ValueError('invalid classification')
        text(body); text(source,2000)
        if evidence == 'user_statement':
            text(quote)
        elif quote:
            raise ValueError('only attributed user statements have a user quote')
        if kind in {'explicit_choice','constraint','preference'} and evidence != 'user_statement':
            raise ValueError('use proposal for inferred user requirements')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected revision required')
        payload = ['save',project,kind,body,source,evidence,quote,expected_revision,supersedes,remote]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._project(db,project,remote)
            writable(db, project)
            digest, prior = self._retry(db,request_id,payload)
            if prior: return prior
            if current['revision'] != expected_revision:
                raise ValueError('revision conflict; reread before editing')
            if supersedes:
                old = db.execute('SELECT id,evidence FROM notes WHERE id=? AND project=?', (supersedes,project)).fetchone()
                replaced = db.execute('SELECT id FROM notes WHERE supersedes=?', (supersedes,)).fetchone()
                if old is None or replaced:
                    raise ValueError('correction target unavailable')
                inherited=self._kind(db,project,supersedes)
                if kind not in {inherited,'correction'}:
                    raise ValueError('correction must preserve target category')
                if (old['evidence']=='user_statement' or inherited in {'constraint','explicit_choice','preference'}) and evidence!='user_statement':
                    raise ValueError('proposal cannot replace attributed user requirement')
            note, revision = 'n-'+uuid.uuid4().hex, expected_revision+1
            db.execute('INSERT INTO notes(id,project,revision,kind,body,source,evidence,quote,supersedes) VALUES (?,?,?,?,?,?,?,?,?)',
                (note,project,revision,kind,body,source,evidence,quote,supersedes))
            db.execute('UPDATE projects SET revision=? WHERE id=?', (revision,project))
            return self._receipt(db,request_id,digest,dict(project=project,note=note,revision=revision,status='saved-draft',binding=False))

    def read(self, project, *, remote=False, offset=0, snapshot=None, operation='resume', mode='delegate', detail='full', task_types=None, decision_factors=None):
        if task_types is not None or decision_factors is not None:
            raise ValueError('scoped classification requires the configured shared canonical route')
        if detail not in {'full','overview'} or (detail=='overview' and (offset != 0 or operation != 'resume')):
            raise ValueError('overview is status only; use full read')
        if snapshot is not None: raise ValueError('snapshot is only supported by the Git route')
        if operation not in {'resume','plan','implement','review'} or mode not in {'delegate','independent','red-team'}:
            raise ValueError('invalid context operation or mode')
        if type(offset) is not int or offset<0: raise ValueError('invalid offset')
        with self.db() as db:
            db.execute('BEGIN')
            current = self._project(db,project,remote)
            fence = db.execute('SELECT generation FROM intake_fences WHERE project=?', (project,)).fetchone()
            notes = [] if detail=='overview' else db.execute('SELECT * FROM notes WHERE project=? AND id NOT IN (SELECT supersedes FROM notes WHERE supersedes IS NOT NULL) ORDER BY revision LIMIT 11 OFFSET ?', (project,offset)).fetchall()
            selected=[]
            for row in notes[:10]:
                item=dict(row)
                item['captured_kind']=item['kind']
                item['kind']=self._kind(db,project,item['id'])
                selected.append(item)
            overview_row=db.execute("SELECT id,revision,body FROM notes WHERE project=? AND kind='proposal' AND evidence='model_inference' AND substr(body,1,?)=? AND id NOT IN (SELECT supersedes FROM notes WHERE supersedes IS NOT NULL) ORDER BY revision DESC LIMIT 1",
                (project,len(OVERVIEW_HEADER),OVERVIEW_HEADER)).fetchone()
            overview=None if overview_row is None else dict(note=overview_row['id'],text=overview_row['body'][len(OVERVIEW_HEADER):],
                current=overview_row['revision']==current['revision'],revision=overview_row['revision'])
        if detail=='overview':
            return dict(project=project,title=current['title'],revision=current['revision'],
                state='draft',authority='model-summary-not-owner-confirmation',binding=False,
                storage=dict(write_state='frozen' if fence else 'active'),read_scope='overview',notes=[],notes_omitted=True,next_offset=None,
                overview_status='missing' if overview is None else 'current' if overview['current'] else 'stale',
                overview=overview if overview and overview['current'] else None,
                context=dict(status='not_evaluated_overview',complete=False),
                implementation_rule='Status display only. Notes and required context were NOT read. Use full read and every page before decisions, implementation or handoff.')
        return dict(project=project,title=current['title'],revision=current['revision'],
            storage=dict(write_state='frozen' if fence else 'active', generation=fence['generation'] if fence else None),
            state='draft',authority='attributed-input-not-owner-confirmation',binding=False,
            notes=selected,next_offset=offset+10 if len(notes)>10 else None,overview=overview,
            source_documents=source_documents(selected),
            implementation_rule='Preserve explicit user choices and constraints; choose other means for the goal. Resolve uncertainty before changing material limits. Read all pages before handoff. Confirmed Contract/Decision/UAT remain separate.')

    def set_remote(self, project, enabled):
        """Trusted local configuration only; deliberately not an MCP tool."""
        if type(enabled) is not bool: raise ValueError('boolean required')
        with self.db() as db:
            self._project(db,project,False)
            writable(db, project)
            db.execute('UPDATE projects SET remote=? WHERE id=?',(int(enabled),project))

    def export(self, project, *, operation='resume', mode='delegate', task_types=None, decision_factors=None):
        if task_types is not None or decision_factors is not None:
            raise ValueError('scoped classification requires the configured shared canonical route')
        view, offset = self.read(project,operation=operation,mode=mode), 0
        while view['next_offset'] is not None:
            offset = view['next_offset']
            page = self.read(project,offset=offset,operation=operation,mode=mode)
            if page['revision'] != view['revision']: raise ValueError('revision changed during read')
            view['notes'] += page['notes']; view['next_offset'] = page['next_offset']
        view['source_documents'] = source_documents(view['notes'])
        base = self.folder(project)
        data = json.dumps(view,ensure_ascii=False,indent=2).encode('utf-8')
        digest=hashlib.sha256(data).hexdigest()
        target = base / '03_context' / f'handoff-r{view["revision"]}-{digest[:16]}.json'
        if target.exists():
            if target.read_bytes()!=data: raise ValueError('handoff conflict')
        else:
            with target.open('xb') as f: f.write(data)
        return dict(path=str(target),sha256=hashlib.sha256(data).hexdigest(),revision=view['revision'])

    def prepare(self, project, *, mode='delegate', task_types=None, decision_factors=None):
        """Local draft for native Task.start. No confirmation or permission implied."""
        exported = self.export(project,operation='plan',mode=mode,
                               task_types=task_types,decision_factors=decision_factors)
        view = json.loads(Path(exported['path']).read_text(encoding='utf-8'))
        if 'context' in view and not view['context'].get('complete'):
            raise ValueError('required context unavailable before native Task handoff')
        if view.get('currentness',{}).get('changed'):
            raise ValueError('project changed during handoff; reread before preparing a contract draft')
        grouped = {kind:[n for n in view['notes'] if n['kind']==kind] for kind in KINDS}
        if not grouped['goal'] or not grouped['acceptance']:
            raise ValueError('explicit goal and acceptance notes required before implementation')
        def attributed(note):
            return note['quote'] if note['evidence']=='user_statement' else note['body']
        draft = dict(goal='\n'.join(attributed(n) for n in grouped['goal']),
            acceptance={'criterion-'+str(i+1):['machine','contract','uat'] for i in range(len(grouped['acceptance']))},
            constraints=[attributed(n) for n in view['notes'] if n['kind'] in {'constraint','explicit_choice'}],
            source=f'intake:{project}:r{view["revision"]}:sha256:{exported["sha256"]}')
        # Include the actual completion text, not meaningless criterion IDs alone.
        draft['goal'] += '\nCompletion criteria:\n' + '\n'.join(
            'criterion-'+str(i+1)+': '+attributed(n) for i,n in enumerate(grouped['acceptance']))
        path = self.handoff_folder(project)/f'contract-draft-r{view["revision"]}-{exported["sha256"][:16]}.json'
        data=json.dumps(draft,ensure_ascii=False,indent=2).encode('utf-8')
        if path.exists():
            if path.read_bytes()!=data: raise ValueError('draft conflict')
        else:
            with path.open('xb') as f: f.write(data)
        return dict(draft=str(path),handoff=exported,confirmed=False,
            next='Local agent independently reviews sources and resolves open questions, then uses nexus.task start with native owner confirmation. No remote approval.')

    def handoff_folder(self, project):
        return self.folder(project)/'03_context'


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=default_root())
    sub=p.add_subparsers(dest='command',required=True)
    listing=sub.add_parser('list')
    listing.add_argument('--query',default='')
    listing.add_argument('--offset',type=int,default=0)
    listing.add_argument('--snapshot')
    listing.add_argument('--include-archived',action='store_true')
    listing.add_argument('--include-merged',action='store_true')
    searching=sub.add_parser('search',help='Search current notes in an explicitly configured shared Git store')
    searching.add_argument('--query',required=True)
    searching.add_argument('--project')
    searching.add_argument('--kinds',nargs='+',choices=sorted(KINDS))
    searching.add_argument('--cursor')
    searching.add_argument('--snapshot')
    for name in ('read','export','prepare'):
        command=sub.add_parser(name)
        command.add_argument('project')
        if name in ('read','export'):
            command.add_argument('--operation',default='resume',choices=['resume','plan','implement','review'])
        command.add_argument('--mode',default='delegate',choices=['delegate','independent','red-team'])
        command.add_argument('--task-types',nargs='*')
        command.add_argument('--decision-factors',nargs='+',choices=['none','owner_values','priority','tradeoff','delegated_decision'])
        if name=='read':
            command.add_argument('--offset',type=int,default=0)
            command.add_argument('--snapshot')
            command.add_argument('--detail',default='full',choices=['full','overview','context','changes','relations'])
            command.add_argument('--since-revision',type=int)
            command.add_argument('--known-snapshot')
            command.add_argument('--known-context-digest')
    lifecycle=sub.add_parser('lifecycle')
    lifecycle.add_argument('project')
    lifecycle.add_argument('--detail',default='status',choices=['status','integrity','deletion_review'])
    for name in ('preview-lifecycle','apply-lifecycle','preview-rule','apply-rule','preview-note-removal','apply-note-removal'):
        sub.add_parser(name).add_argument('json_file',type=Path)
    sub.add_parser('status',help='Read capabilities from an explicit canonical Git root')
    sub.add_parser('review-start',help='Review existing projects before a shared create').add_argument('json_file',type=Path)
    for name in ('create','save'):
        command=sub.add_parser(name)
        command.add_argument('json_file',type=Path)
        if name=='create':command.add_argument('--local-only',action='store_true',help='Keep this new project out of the authorized ChatGPT connection')
    a=p.parse_args()
    from nexus.git_intake import open_intake
    w=open_intake(a.root)
    if a.command=='list':
        extra={k:True for k,v in dict(include_archived=a.include_archived,include_merged=a.include_merged).items() if v}
        result=w.list(a.query,offset=a.offset,snapshot=a.snapshot,**extra)
    elif a.command in ('lifecycle','preview-lifecycle','apply-lifecycle','preview-rule','apply-rule','preview-note-removal','apply-note-removal'):
        from nexus.git_intake import GitIntake
        payload=dict(project=a.project,detail=a.detail) if a.command=='lifecycle' else json.loads(a.json_file.read_text(encoding='utf-8'))
        target=w if isinstance(w,GitIntake) else w._target(payload['project'])
        if target is None:raise ValueError('lifecycle requires a configured shared canonical project')
        result=getattr(target,a.command.replace('-','_'))(**payload)
    elif a.command in ('status','review-start'):
        from nexus.git_intake import GitIntake
        if not isinstance(w,GitIntake):w=w._default_target()
        if w is None:raise ValueError('use the trusted canonical Git root for project-start review or service status')
        result=w.capabilities() if a.command=='status' else w.review_start(**json.loads(a.json_file.read_text(encoding='utf-8')))
    elif a.command=='search':
        from nexus.git_intake import GitIntake
        if not isinstance(w,GitIntake):
            if not a.project: raise ValueError('shared search needs an explicit canonical Git root or routed project; local-only notes are not searched')
            w=w._target(a.project)
            if w is None: raise ValueError('shared search unavailable for local-only project')
        result=w.search(a.query,project=a.project,kinds=a.kinds,cursor=a.cursor,snapshot=a.snapshot)
    elif a.command=='read':
        extra=dict(since_revision=a.since_revision,known_snapshot=a.known_snapshot,
                   known_context_digest=a.known_context_digest) if a.detail=='changes' else {}
        if a.detail=='full' and a.known_context_digest is not None:extra['known_context_digest']=a.known_context_digest
        if a.task_types is not None:extra['task_types']=a.task_types
        if a.decision_factors is not None:extra['decision_factors']=a.decision_factors
        result=w.read(a.project,offset=a.offset,snapshot=a.snapshot,operation=a.operation,mode=a.mode,detail=a.detail,**extra)
    elif a.command=='export':
        extra={k:v for k,v in dict(task_types=a.task_types,decision_factors=a.decision_factors).items() if v is not None}
        result=w.export(a.project,operation=a.operation,mode=a.mode,**extra)
    elif a.command=='prepare':
        result=w.prepare(a.project,mode=a.mode,task_types=a.task_types,decision_factors=a.decision_factors)
    else:
        payload=json.loads(a.json_file.read_text(encoding='utf-8'))
        if a.command=='create':
            if 'remote' in payload:raise ValueError('use --local-only to choose visibility')
            payload['remote']=not a.local_only
        result=getattr(w,a.command)(**payload)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
