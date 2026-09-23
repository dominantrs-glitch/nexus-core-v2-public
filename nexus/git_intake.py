"""Opt-in owner-local client using the SAME Git logic as the cloud Worker.

Only a trusted local canonical-git.json selects this route. No data/network
fallback to SQLite on failure. Authentication key bytes never enter Python,
argv, logs or the conversation. Production activation remains a separate step.
"""
import json
from pathlib import Path
import shutil
import subprocess
from time import perf_counter
from nexus.intake import Intake
from nexus.source_documents import source_documents


class GitIntake:
    def __init__(self, root, call=None):
        self.root = Path(root).resolve()
        self.config = self.root/'canonical-git.json'
        self._call = call or self._invoke

    def _invoke(self, operation, args):
        from nexus.efficiency import record
        started, response = perf_counter(), None
        try:
            response = self._invoke_measured(operation, args)
            return response
        finally:
            record(operation, args, response, (perf_counter() - started) * 1000)

    def _invoke_measured(self, operation, args):
        base = Path(__file__).resolve().parents[1]
        runner = base/'cloudflare/dist/git-local.mjs'
        node = shutil.which('node')
        if not node or not runner.is_file() or not self.config.is_file():
            raise ValueError('canonical Git client unavailable; no local fallback')
        sources = ('git-local.ts','git-store.ts','git-search.ts','git-relations.ts','git-work.ts','git-binary.ts','git-context.ts','git-original.ts','git-core.ts','git-native-context.ts','git-native-learning.ts','github-store.ts','relay-common.ts','source-documents.ts')
        if any((base/'cloudflare/src'/name).stat().st_mtime > runner.stat().st_mtime for name in sources):
            raise ValueError('canonical Git client build is stale; rebuild before use')
        try:
            result = subprocess.run([node,str(runner),str(self.config)],
                input=json.dumps(dict(operation=operation,args=args),ensure_ascii=False),
                capture_output=True,encoding='utf-8',timeout=90,check=False)
            if len(result.stdout.encode()) > 2*1024*1024:
                raise ValueError('canonical response too large')
            response=json.loads(result.stdout)
        except (OSError,subprocess.TimeoutExpired,json.JSONDecodeError):
            raise ValueError('canonical unavailable or outcome unknown; retry the same request') from None
        if result.returncode or response.get('ok') is not True:
            reason=response.get('reason','canonical unavailable or outcome unknown')
            if not isinstance(reason,str) or len(reason)>150:
                reason='canonical unavailable or outcome unknown'
            raise ValueError(reason)
        return response['result']

    def list(self, query='', *, remote=False, offset=0, snapshot=None):
        args=dict(query=query,offset=offset)
        if snapshot is not None: args['snapshot']=snapshot
        return self._call('list',args)

    def search(self, query, *, project=None, kinds=None, cursor=None, snapshot=None):
        args=dict(query=query)
        for key,value in dict(project=project,kinds=kinds,cursor=cursor,snapshot=snapshot).items():
            if value is not None: args[key]=value
        return self._call('search',args)

    def original(self, project, *, operation='resume', detail='list', source_project=None,
                 original=None, revision=None, sha256=None, offset=0, snapshot=None):
        args=dict(project=project,operation=operation,detail=detail,offset=offset)
        for key,value in dict(source_project=source_project,original=original,revision=revision,
                              sha256=sha256,snapshot=snapshot).items():
            if value is not None: args[key]=value
        return self._call('original',args)

    def save_relation(self, **args):
        return self._call('saveRelation',args)

    def binary(self, **args):
        return self._call('binary', args)

    def work(self, **args):
        return self._call('work', args)

    def save_work(self, **args):
        return self._call('saveWork', args)

    def save_hours(self, **args):
        return self._call('saveHours', args)

    def read(self, project, *, remote=False, offset=0, snapshot=None, operation='resume',mode='delegate',detail='full',
             since_revision=None,known_snapshot=None,known_context_digest=None):
        args=dict(project=project,offset=offset,operation=operation,mode=mode)
        if detail != 'full':args['detail']=detail
        if snapshot is not None: args['snapshot']=snapshot
        if since_revision is not None:args['since_revision']=since_revision
        if known_snapshot is not None:args['known_snapshot']=known_snapshot
        if known_context_digest is not None:args['known_context_digest']=known_context_digest
        return self._call('read',args)

    def create(self, title,source,request_id, *, remote=False):
        # Cloud route only supports explicitly shared projects. Never turn a local
        # confidential create into an external write by dropping its scope flag.
        if not remote: raise ValueError('local-only creation unavailable on cloud route')
        return self._call('create',dict(title=title,source=source,request_id=request_id))

    def save(self,project,kind,body,source,evidence,quote,expected_revision,request_id, *, supersedes=None,remote=False):
        return self._call('save',dict(project=project,kind=kind,body=body,source=source,evidence=evidence,
            quote=quote,expected_revision=expected_revision,request_id=request_id,supersedes=supersedes))

    def set_remote(self,*args,**kwargs):
        raise ValueError('cloud visibility requires trusted administrator configuration')

    def folder(self, project):
        # Local originals are not implied to exist merely because cloud notes do.
        raise ValueError('cloud project has no configured local original folder')

    def export(self, project, *, operation='resume', mode='delegate'):
        import hashlib
        from nexus.intake import identifier
        identifier(project)
        view=self.read(project,operation=operation,mode=mode)
        if operation != 'resume' and not view.get('context',{}).get('complete'):
            raise ValueError('required context unavailable for requested export operation')
        while view['next_offset'] is not None:
            page=self.read(project,offset=view['next_offset'],snapshot=view['snapshot'],operation=operation,mode=mode)
            if page['snapshot']!=view['snapshot'] or page['generation']!=view['generation']:
                raise ValueError('canonical changed during export; reread')
            if operation != 'resume' and not page.get('context',{}).get('complete'):
                raise ValueError('required context unavailable during export')
            view['notes']+=page['notes'];view['next_offset']=page['next_offset']
        if 'source_documents' in view:
            view['source_documents']=source_documents(view['notes'])
        data=json.dumps(view,ensure_ascii=False,indent=2).encode()
        checksum=hashlib.sha256(data).hexdigest()
        directory=self.root/'exports'
        if not directory.resolve().is_relative_to(self.root):raise ValueError('storage boundary')
        directory.mkdir(exist_ok=True)
        target=directory/f'{project}-r{view["revision"]}-{checksum[:16]}.json'
        if target.exists():
            if target.read_bytes()!=data:raise ValueError('export conflict')
        else:
            with target.open('xb') as stream: stream.write(data)
        return dict(path=str(target),sha256=checksum,revision=view['revision'],snapshot=view['snapshot'],generation=view['generation'])

    def prepare(self, project):
        from nexus.intake import Intake
        return Intake.prepare(self,project)

    def handoff_folder(self, project):
        from nexus.intake import identifier
        directory=self.root/'handoffs'/identifier(project)
        if not directory.resolve().is_relative_to(self.root):raise ValueError('storage boundary')
        directory.mkdir(parents=True,exist_ok=True)
        return directory


class RoutedIntake(Intake):
    """Only locally registered, already-fenced projects use the cloud route.

    Resolve the registry per call so newly started processes do not retain a
    routing cache. Already-loaded pre-upgrade services still require a restart.
    """
    def _target(self, project, remote=False):
        with self.db() as db:
            self._project(db,project,remote)
            row=db.execute('SELECT * FROM intake_routes WHERE project=?',(project,)).fetchone()
        if row is None:return None
        root=Path(row['config_root'])
        try:
            config=json.loads((root/'canonical-git.json').read_text('utf-8'))
            if config['generation']!=row['generation']:raise ValueError('route generation mismatch')
        except (OSError,KeyError,json.JSONDecodeError):
            raise ValueError('canonical route unavailable; no local fallback') from None
        return GitIntake(root)

    def read(self,project,**kwargs):
        target=self._target(project,kwargs.get('remote',False))
        return target.read(project,**kwargs) if target else super().read(project,**kwargs)

    def save(self,project,*args,**kwargs):
        target=self._target(project,kwargs.get('remote',False))
        return target.save(project,*args,**kwargs) if target else super().save(project,*args,**kwargs)

    def create(self,title,source,request_id,*,remote=False):
        # A replay of a migrated CREATE follows its old receipt to the same project.
        with self.db() as db:
            _,prior=self._retry(db,request_id,['create',title,source,remote])
        if prior:
            target=self._target(prior['project'],remote)
            if target:
                # This is an existing shared project's replay, not new enrollment.
                return target.create(title,source,request_id,remote=True)
        return super().create(title,source,request_id,remote=remote)

    def list(self,query='',*,remote=False,offset=0,snapshot=None):
        from nexus.git_migration import digest
        if not isinstance(query,str) or len(query)>200 or type(offset) is not int or offset<0:
            raise ValueError('invalid query')
        query=query.strip()
        with self.db() as db:
            db.execute('BEGIN')
            routes=[dict(r) for r in db.execute('SELECT r.* FROM intake_routes r JOIN projects p ON p.id=r.project WHERE (?=0 OR p.remote=1) ORDER BY r.project',(int(remote),))]
            if not routes:return super().list(query,remote=remote,offset=offset,snapshot=snapshot)
            rows=[dict(r) for r in db.execute('SELECT id,title,revision FROM projects WHERE (?=0 OR remote=1) AND instr(lower(title),lower(?))>0 ORDER BY id',
                                            (int(remote),query))]
        routed={r['project'] for r in routes}
        result=[r for r in rows if r['id'] not in routed]
        versions=[]
        # One snapshot-paged list per configured repository; never per note.
        for config_root in sorted({r['config_root'] for r in routes}):
            applicable=[r for r in routes if r['config_root']==config_root]
            target=self._target(applicable[0]['project'],remote)
            page=target.list(query)
            if any(r['generation']!=page['generation'] for r in applicable):
                raise ValueError('route generation mismatch')
            versions.append((config_root,page['generation'],page['snapshot']))
            allowed={r['project'] for r in applicable}
            while True:
                result.extend(p for p in page['projects'] if p['id'] in allowed)
                if page['next_offset'] is None:break
                page=target.list(query,offset=page['next_offset'],snapshot=page['snapshot'])
        result.sort(key=lambda p:p['id'])
        stamp=digest(dict(projects=result,versions=versions,remote=remote,query=query))
        if (offset and snapshot is None) or (snapshot is not None and snapshot!=stamp):
            raise ValueError('snapshot changed or missing; restart project listing')
        return dict(projects=result[offset:offset+25],next_offset=offset+25 if len(result)>offset+25 else None,snapshot=stamp,
                    search=dict(status='not_searched' if not query else 'matches' if result else 'no_match',
                                scope='project_titles',total_matches=len(result)))

    def export(self, project, *, operation='resume', mode='delegate'):
        target=self._target(project)
        return target.export(project,operation=operation,mode=mode) if target else super().export(project,operation=operation,mode=mode)

    def prepare(self, project):
        target=self._target(project)
        return target.prepare(project) if target else super().prepare(project)

    def folder(self, project):
        # During local create the ID has not been inserted yet; do not require it.
        with self.db() as db:
            routed=db.execute('SELECT 1 FROM intake_routes WHERE project=?',(project,)).fetchone()
        if routed:raise ValueError('migrated project originals are not configured locally')
        return super().folder(project)


def open_intake(root):
    """No automatic enrollment or credentials. Unconfigured roots stay local."""
    root=Path(root)
    return GitIntake(root) if (root/'canonical-git.json').exists() else RoutedIntake(root)


def register_route(store,project,source_generation,config_root,verified_snapshot):
    """Trusted local final routing step, after history/receipt transfer verification.

    Destination must still be frozen at the reviewed commit. This only switches
    reads/replays; the administrator separately enables destination writes after
    restarting old services. The old source fence remains permanently in place.
    """
    config_root=Path(config_root).resolve()
    config=json.loads((config_root/'canonical-git.json').read_text('utf-8'))
    view=GitIntake(config_root).read(project)
    if (view['snapshot']!=verified_snapshot or view['generation']!=config['generation']
            or view['storage']['write_state']!='frozen'):
        raise ValueError('destination changed or not frozen; verify again')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        source=store._project(db,project,True)
        fence=db.execute('SELECT * FROM intake_fences WHERE project=?',(project,)).fetchone()
        if not fence or fence['generation']!=source_generation or source['revision']!=view['revision']:
            raise ValueError('source changed or not frozen; verify again')
        existing=db.execute('SELECT * FROM intake_routes WHERE project=?',(project,)).fetchone()
        if existing:
            if existing['generation']!=view['generation'] or existing['config_root']!=str(config_root):
                raise ValueError('route already set; no automatic replacement')
        else:
            db.execute('INSERT INTO intake_routes VALUES (?,?,?)',(project,view['generation'],str(config_root)))
    return dict(project=project,generation=view['generation'],snapshot=view['snapshot'],
                state='routed-destination-frozen',old_source_write_state='frozen')
