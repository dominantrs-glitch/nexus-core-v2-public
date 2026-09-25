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


class CanonicalUnavailable(ValueError):
    """Sanitized upstream diagnosis; never contains an input or credential."""
    def __init__(self, reason, diagnostic=None):
        self.diagnostic = diagnostic
        super().__init__(reason + (('; retry after ' + str(diagnostic['retry_after_seconds']) + ' seconds')
                                  if diagnostic and diagnostic.get('retryable') and
                                  type(diagnostic.get('retry_after_seconds')) is int else ''))


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
        sources = ('git-local.ts','git-store.ts','git-rules.ts','git-note-removal.ts','git-validation.ts','git-lifecycle.ts','git-start.ts','git-search.ts','git-relations.ts','git-work.ts','git-daily.ts','git-calendar.ts','git-binary.ts','git-context.ts','git-original.ts','git-core.ts','git-native-context.ts','git-native-learning.ts','github-store.ts','relay-common.ts','source-documents.ts')
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
            diagnostic=response.get('diagnostic')
            if not isinstance(diagnostic,dict):diagnostic=None
            raise CanonicalUnavailable(reason,diagnostic)
        return response['result']

    def list(self, query='', *, remote=False, offset=0, snapshot=None, include_archived=False, include_merged=False):
        args=dict(query=query,offset=offset)
        if snapshot is not None: args['snapshot']=snapshot
        if include_archived:args['include_archived']=True
        if include_merged:args['include_merged']=True
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

    def lifecycle(self, **args):
        return self._call('lifecycle', args)

    def preview_lifecycle(self, **args):
        return self._call('previewLifecycle', args)

    def apply_lifecycle(self, **args):
        return self._call('applyLifecycle', args)

    def review_start(self, **args):
        return self._call('reviewStart', args)

    def capabilities(self):
        return self._call('capabilities', {})

    def preview_rule(self, **args):
        return self._call('previewRule', args)

    def apply_rule(self, **args):
        return self._call('applyRule', args)

    def preview_note_removal(self, **args):
        return self._call('previewNoteRemoval', args)

    def apply_note_removal(self, **args):
        return self._call('applyNoteRemoval', args)

    def read(self, project, *, remote=False, offset=0, snapshot=None, operation='resume',mode='delegate',detail='full',
             since_revision=None,known_snapshot=None,known_context_digest=None,task_types=None,decision_factors=None):
        args=dict(project=project,offset=offset,operation=operation,mode=mode)
        if detail != 'full':args['detail']=detail
        if snapshot is not None: args['snapshot']=snapshot
        if since_revision is not None:args['since_revision']=since_revision
        if known_snapshot is not None:args['known_snapshot']=known_snapshot
        if known_context_digest is not None:args['known_context_digest']=known_context_digest
        if task_types is not None:args['task_types']=task_types
        if decision_factors is not None:args['decision_factors']=decision_factors
        return self._call('read',args)

    def create(self, title,source,request_id, *, remote=False,review=None):
        # Cloud route only supports explicitly shared projects. Never turn a local
        # confidential create into an external write by dropping its scope flag.
        if not remote: raise ValueError('local-only creation unavailable on cloud route')
        args=dict(title=title,source=source,request_id=request_id)
        if review is not None:args['review']=review
        return self._call('create',args)

    def save(self,project,kind,body,source,evidence,quote,expected_revision,request_id, *, supersedes=None,remote=False):
        return self._call('save',dict(project=project,kind=kind,body=body,source=source,evidence=evidence,
            quote=quote,expected_revision=expected_revision,request_id=request_id,supersedes=supersedes))

    def set_remote(self,*args,**kwargs):
        raise ValueError('cloud visibility requires trusted administrator configuration')

    def folder(self, project):
        # Local originals are not implied to exist merely because cloud notes do.
        raise ValueError('cloud project has no configured local original folder')

    def export(self, project, *, operation='resume', mode='delegate', task_types=None, decision_factors=None):
        import hashlib
        from nexus.intake import identifier
        identifier(project)
        context_args=dict(operation=operation,mode=mode,task_types=task_types,decision_factors=decision_factors)
        view=self.read(project,**context_args)
        if operation != 'resume' and not view.get('context',{}).get('complete'):
            raise ValueError('required context unavailable for requested export operation')
        while view['next_offset'] is not None:
            page=self.read(project,offset=view['next_offset'],snapshot=view['snapshot'],
                           known_context_digest=view.get('context_digest'),**context_args)
            if page['snapshot']!=view['snapshot'] or page['generation']!=view['generation']:
                raise ValueError('canonical changed during export; reread')
            if operation != 'resume' and not page.get('context',{}).get('complete'):
                raise ValueError('required context unavailable during export')
            view['notes']+=page['notes'];view['next_offset']=page['next_offset']
            if page.get('withdrawn_notes'):
                view.setdefault('withdrawn_notes', []).extend(page['withdrawn_notes'])
            if 'currentness' in page:view['currentness']=page['currentness']
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

    def prepare(self, project, **kwargs):
        from nexus.intake import Intake
        return Intake.prepare(self,project,**kwargs)

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
    def _default_target(self):
        """Explicit owner-local opt-in to an already registered shared store.

        Absent configuration preserves the old project-specific migration scope.
        Broken configuration fails closed, including during new project creation.
        """
        with self.db() as db:
            row=db.execute('SELECT config_root,generation FROM intake_default_route WHERE singleton=1').fetchone()
        if row is None:return None
        try:
            selection=dict(row)
            root=Path(selection['config_root']).resolve()
            config=json.loads((root/'canonical-git.json').read_text('utf-8'))
            with self.db() as db:
                registered=db.execute('SELECT 1 FROM intake_routes WHERE config_root=? AND generation=?',
                                      (str(root),selection['generation'])).fetchone()
            if not registered or config['generation']!=selection['generation']:raise ValueError()
        except (OSError,KeyError,TypeError,ValueError):
            raise ValueError('default canonical route unavailable; no local fallback') from None
        return GitIntake(root)

    def _target(self, project, remote=False):
        from nexus.intake import identifier
        identifier(project)
        with self.db() as db:
            local=db.execute('SELECT remote FROM projects WHERE id=?',(project,)).fetchone()
            if local is not None and remote and not local['remote']:raise ValueError('project unavailable')
            row=db.execute('SELECT * FROM intake_routes WHERE project=?',(project,)).fetchone()
        if local is None:
            target=self._default_target()
            if target:return target  # Git verifies the current catalog ACL itself.
            raise ValueError('project unavailable')
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

    def create(self,title,source,request_id,*,remote=False,review=None):
        # A replay of a migrated CREATE follows its old receipt to the same project.
        with self.db() as db:
            _,prior=self._retry(db,request_id,['create',title,source,remote])
        if prior:
            target=self._target(prior['project'],remote)
            if target:
                # This is an existing shared project's replay, not new enrollment.
                return target.create(title,source,request_id,remote=True,review=review)
        if remote and prior is None:
            target=self._default_target()
            if target:return target.create(title,source,request_id,remote=True,review=review)
        if review is not None:
            raise ValueError('reviewed shared creation requires the trusted canonical Git root; no local duplicate is created')
        return super().create(title,source,request_id,remote=remote)

    def list(self,query='',*,remote=False,offset=0,snapshot=None,include_archived=False,include_merged=False):
        from nexus.git_migration import digest
        if not isinstance(query,str) or len(query)>200 or type(offset) is not int or offset<0:
            raise ValueError('invalid query')
        query=query.strip()
        default=self._default_target()
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
            page=target.list(query,include_archived=include_archived,include_merged=include_merged)
            if any(r['generation']!=page['generation'] for r in applicable):
                raise ValueError('route generation mismatch')
            versions.append((config_root,page['generation'],page['snapshot']))
            allowed={r['project'] for r in applicable}
            while True:
                result.extend(p for p in page['projects'] if p['id'] in allowed or
                              (default is not None and default.root==Path(config_root).resolve()))
                if page['next_offset'] is None:break
                page=target.list(query,offset=page['next_offset'],snapshot=page['snapshot'],include_archived=include_archived,include_merged=include_merged)
        result.sort(key=lambda p:p['id'])
        if len({p['id'] for p in result})!=len(result):raise ValueError('duplicate project identity across canonical routes; review configuration')
        stamp=digest(dict(projects=result,versions=versions,remote=remote,query=query))
        if (offset and snapshot is None) or (snapshot is not None and snapshot!=stamp):
            raise ValueError('snapshot changed or missing; restart project listing')
        return dict(projects=result[offset:offset+25],next_offset=offset+25 if len(result)>offset+25 else None,snapshot=stamp,
                    search=dict(status='not_searched' if not query else 'matches' if result else 'no_match',
                                scope='project_titles',total_matches=len(result)))

    def export(self, project, *, operation='resume', mode='delegate', **kwargs):
        target=self._target(project)
        return target.export(project,operation=operation,mode=mode,**kwargs) if target else super().export(project,operation=operation,mode=mode,**kwargs)

    def prepare(self, project, **kwargs):
        target=self._target(project)
        return target.prepare(project,**kwargs) if target else super().prepare(project,**kwargs)

    def folder(self, project):
        with self.db() as db:
            routed=db.execute('SELECT 1 FROM intake_routes WHERE project=?',(project,)).fetchone()
            local=db.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone()
        if routed or (local is None and self._default_target() is not None):
            raise ValueError('shared project originals are not configured locally')
        return super().folder(project)


def open_intake(root):
    """No automatic enrollment or credentials. Unconfigured roots stay local."""
    root=Path(root)
    return GitIntake(root) if (root/'canonical-git.json').exists() else RoutedIntake(root)


def register_default_route(store, config_root):
    """Trusted local opt-in; does not enable cloud creation or change cloud ACLs."""
    config_root=Path(config_root).resolve()
    config=json.loads((config_root/'canonical-git.json').read_text('utf-8'))
    with store.db() as db:
        if not db.execute('SELECT 1 FROM intake_routes WHERE config_root=? AND generation=?',
                          (str(config_root),config['generation'])).fetchone():
            raise ValueError('default route must already be registered and verified')
    status=GitIntake(config_root).capabilities()
    if not status['features']['projects']['creation_enabled']:
        raise ValueError('canonical creation is not enabled; no routing change applied')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT config_root,generation FROM intake_default_route WHERE singleton=1').fetchone()
        if old and (old['config_root']!=str(config_root) or old['generation']!=config['generation']):
            raise ValueError('default canonical already configured; no automatic replacement')
        if not db.execute('SELECT 1 FROM intake_routes WHERE config_root=? AND generation=?',
                          (str(config_root),config['generation'])).fetchone():
            raise ValueError('registered route changed before activation')
        if not old:db.execute('INSERT INTO intake_default_route VALUES (1,?,?)',(config['generation'],str(config_root)))
    return dict(status='default-shared-route-enabled',generation=config['generation'],
                legacy_shared_creation='fenced',local_private_creation='unchanged')


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
