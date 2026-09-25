"""Byte-exact owner-local legacy archive. No upload, merge, execution or deletion."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from nexus.environment_recovery import relative, safe_file

MAX_BYTES = 2 * 1024**3
EXTRA_CHUNK = 32 * 1024**2


def entries(archive):
    root=Path(archive)
    value=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    extra=root/'extra-manifest.json'
    return value['entries']+(json.loads(extra.read_text(encoding='utf-8'))['entries'] if extra.exists() else [])


def capture_extra(repository, archive, selection):
    """Explicit ignored originals; stream large originals into exact local chunks."""
    repo=Path(repository).resolve(strict=True);root=Path(archive).resolve(strict=True)
    if (root/'extra-manifest.json').exists():raise ValueError('extra capture already exists')
    rows=[];total=0
    for item in selection:
        name=str(relative(item['path']));source=repo.joinpath(*relative(name).parts)
        # Same path/link checks as regular inputs, but allow streaming >256 MiB.
        if any(p.is_symlink() or p.is_junction() for p in [source,*source.parents]):raise ValueError('linked original')
        before=source.stat()
        if not source.is_file() or before.st_size>2*1024**3:raise ValueError('oversized original')
        total+=before.st_size
        if total>8*1024**3:raise ValueError('extra capture budget exceeded')
        full=hashlib.sha256();chunks=[]
        with source.open('rb') as stream:
            while data:=stream.read(EXTRA_CHUNK):
                full.update(data);sha=hashlib.sha256(data).hexdigest();target=root/'blobs'/sha
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:raise ValueError('archive collision')
                else:target.write_bytes(data)
                chunks.append(dict(sha256=sha,bytes=len(data)))
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('original changed during capture')
        parts=Path(name).parts
        rows.append(dict(id='legacy-'+hashlib.sha256(('ignored-local\0'+name).encode()).hexdigest()[:24],
                         path=name,layer='ignored-local',bytes=before.st_size,sha256=full.hexdigest(),chunks=chunks,
                         legacy_project=parts[1] if len(parts)>2 and parts[0]=='projects' else None,
                         classification='owner-local-only; no external sharing inferred'))
    with (root/'extra-manifest.json').open('x',encoding='utf-8') as out:
        json.dump(dict(schema=1,captured_at=datetime.now(timezone.utc).isoformat(),entries=rows,
                       authority='historical-source-not-current-instruction',external_upload=False),out,ensure_ascii=False,indent=2)
    return verify(root)


def asset_kind(path):
    """Navigation hints only; a folder name cannot establish authority/currentness."""
    parts = Path(path).parts
    lower = path.casefold()
    if Path(path).name.casefold() in {'.env', 'credentials.json', 'token.json'} or lower.endswith(('.pem', '.key')):
        return 'private-configuration-review'
    for folder, kind in [('01_raw','original'), ('05_output','output'), ('04_work','intermediate'),
                         ('06_app','application'), ('03_context','context'), ('07_logs','history'), ('08_archive','archive')]:
        if folder in parts:return kind
    return 'other'


def catalog(archive, repository=None):
    """Small searchable catalog plus ignored-file coverage, without opening secrets."""
    root=Path(archive).resolve(strict=True)
    value=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    rows=[dict(id=r['id'],path=r['path'],layer=r['layer'],kind=asset_kind(r['path']),
               sha256=r.get('sha256'),bytes=r.get('bytes'),deleted=r.get('deleted',False),
               project=r.get('legacy_project'),authority=value['authority']) for r in entries(root)]
    ignored=[]
    captured_ignored={r['path'] for r in rows if r['layer']=='ignored-local'}
    if repository:
        for raw in git(repository,'ls-files','--others','--ignored','--exclude-standard','-z').split(b'\0'):
            if not raw:continue
            name=raw.decode('utf-8');parts=Path(name).parts
            if name in captured_ignored:continue
            category='regenerable-dependency-or-cache' if any(p in {'.venv','venv','node_modules','__pycache__','.pytest_cache','.ruff_cache'} for p in parts) else 'local-only-review-required'
            ignored.append(dict(path=name,classification=category))
    result=dict(schema=1,entries=rows,ignored=ignored,external_upload=False,
                classifications='path-derived hints; not owner approval or current source selection',
                deletion_ready=False)
    with (root/'catalog.local.json').open('w',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
    from collections import Counter
    return dict(entries=len(rows),kinds=dict(Counter(r['kind'] for r in rows)),
                ignored=dict(Counter(r['classification'] for r in ignored)),deletion_ready=False)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, timeout=90)


def inventory(repo, refs=('origin/main', 'HEAD')):
    repo=Path(repo).resolve(strict=True)
    rows, checkpoints=[],{}
    for ref in refs:
        if not isinstance(ref,str) or ref.startswith('-'):raise ValueError('invalid ref')
        commit=git(repo,'rev-parse','--verify',ref+'^{commit}').decode().strip()
        checkpoints[ref]=commit
        for line in git(repo,'ls-tree','-r','-l','-z',commit).split(b'\0'):
            if not line:continue
            header,path=line.split(b'\t',1);mode,kind,blob,size=header.decode().split()
            if kind!='blob' or mode not in {'100644','100755'}:raise ValueError('linked or unsupported tracked original')
            name=str(relative(path.decode('utf-8')))
            rows.append(dict(layer=ref,path=name,blob=blob,bytes=int(size),commit=commit))
    # Preserve modifications separately, never choosing them over remote history.
    changed=set(git(repo,'diff','HEAD','--name-only','-z').split(b'\0'))
    changed.update(git(repo,'ls-files','--others','--exclude-standard','-z').split(b'\0'))
    for raw in sorted(changed):
        if not raw:continue
        name=str(relative(raw.decode('utf-8')));path=repo.joinpath(*relative(name).parts)
        if not path.exists():rows.append(dict(layer='working-tree',path=name,deleted=True));continue
        safe_file(path,repo)
        rows.append(dict(layer='working-tree',path=name,bytes=path.stat().st_size))
    ignored=git(repo,'ls-files','--others','--ignored','--exclude-standard','-z').split(b'\0')
    return dict(schema=1,repository=str(repo),checkpoints=checkpoints,entries=rows,
                ignored_files=sum(bool(x) for x in ignored),ignored_copied=False,
                authority='historical-source-not-current-instruction',external_upload=False)


def capture(repo, destination, refs=('origin/main','HEAD')):
    repo=Path(repo).resolve(strict=True);destination=Path(destination).resolve()
    if destination.is_relative_to(repo):raise ValueError('archive must be outside the old repository')
    plan=inventory(repo,refs)
    unique={r['blob']:r['bytes'] for r in plan['entries'] if 'blob' in r}
    if sum(unique.values())+sum(r.get('bytes',0) for r in plan['entries'] if 'blob' not in r)>MAX_BYTES:
        raise ValueError('legacy archive budget exceeded')
    destination.mkdir(parents=True,exist_ok=False);(destination/'blobs').mkdir()
    saved={};total=0
    process=subprocess.Popen(['git','-C',str(repo),'cat-file','--batch'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        for blob,size in unique.items():
            process.stdin.write((blob+'\n').encode());process.stdin.flush()
            header=process.stdout.readline().decode().split()
            if header!=[blob,'blob',str(size)]:raise ValueError('legacy blob unavailable')
            data=process.stdout.read(size)
            if process.stdout.read(1)!=b'\n' or len(data)!=size:raise ValueError('legacy blob incomplete')
            if hashlib.sha1(b'blob '+str(size).encode()+b'\0'+data).hexdigest()!=blob:raise ValueError('legacy blob changed')
            sha=hashlib.sha256(data).hexdigest();target=destination/'blobs'/sha
            if not target.exists():target.write_bytes(data);total+=size
            saved[blob]=sha
    finally:
        process.stdin.close();process.stdout.close();process.wait(timeout=20);process.stderr.close()
    for index,row in enumerate(plan['entries']):
        row['id']='legacy-'+hashlib.sha256((row['layer']+'\0'+row['path']).encode()).hexdigest()[:24]
        parts=Path(row['path']).parts
        row['legacy_project']=parts[1] if len(parts)>2 and parts[0]=='projects' else None
        if row.get('deleted'):continue
        if 'blob' in row:row['sha256']=saved[row['blob']]
        else:
            path=safe_file(repo.joinpath(*relative(row['path']).parts),repo);data=path.read_bytes()
            if data!=path.read_bytes() or len(data)!=row['bytes']:raise ValueError('working file changed during archive')
            row['sha256']=hashlib.sha256(data).hexdigest();target=destination/'blobs'/row['sha256']
            if not target.exists():target.write_bytes(data);total+=len(data)
        row['classification']='local-only-source; share only after project and sensitivity review'
    if inventory(repo,refs)['entries']!=[{k:v for k,v in row.items() if k not in {'id','sha256','legacy_project','classification'}} for row in plan['entries']]:
        raise ValueError('legacy source changed during capture; review a new capture')
    plan.update(captured_at=datetime.now(timezone.utc).isoformat(),unique_bytes=total,unique_blobs=len(list((destination/'blobs').iterdir())))
    (destination/'manifest.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    return verify(destination)


def verify(archive):
    archive=Path(archive).resolve(strict=True)
    value=json.loads((archive/'manifest.json').read_text(encoding='utf-8'));checked={}
    all_rows=entries(archive)
    for row in all_rows:
        relative(row['path'])
        if row.get('deleted'):continue
        sha=row['sha256']
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('invalid blob identity')
        full=hashlib.sha256();size=0
        for ref in row.get('chunks',[dict(sha256=sha,bytes=row['bytes'])]):
            part=ref['sha256']
            if len(part)!=64 or any(c not in '0123456789abcdef' for c in part):raise ValueError('invalid chunk identity')
            data=safe_file(archive/'blobs'/part,archive).read_bytes()
            if hashlib.sha256(data).hexdigest()!=part or len(data)!=ref['bytes']:raise ValueError('archived original mismatch')
            checked[part]=len(data);full.update(data);size+=len(data)
        if size!=row['bytes'] or full.hexdigest()!=sha:raise ValueError('archived size or chunk order mismatch')
    return dict(status='all_selected_bytes_verified',entries=len(all_rows),unique_blobs=len(checked),
                bytes=sum(checked.values()),checkpoints=value['checkpoints'],ignored_files=value['ignored_files'],
                external_upload=False,old_repository_deleted=False,scope='selected Git refs, local changes and explicitly selected ignored originals; excluded runtimes/other local files remain at source')


def extract(archive, identity, destination):
    archive=Path(archive).resolve(strict=True);destination=Path(destination)
    value=json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
    row=next((r for r in entries(archive) if r['id']==identity and not r.get('deleted')),None)
    if row is None:raise ValueError('original unavailable')
    chunks=row.get('chunks',[dict(sha256=row['sha256'],bytes=row['bytes'])]);full=hashlib.sha256();size=0
    for ref in chunks:
        data=safe_file(archive/'blobs'/str(relative(ref['sha256'])),archive).read_bytes()
        if len(data)!=ref['bytes'] or hashlib.sha256(data).hexdigest()!=ref['sha256']:raise ValueError('original unavailable')
        full.update(data);size+=len(data)
    if size!=row['bytes'] or full.hexdigest()!=row['sha256']:raise ValueError('original unavailable')
    with destination.open('xb') as out:
        for ref in chunks:out.write((archive/'blobs'/ref['sha256']).read_bytes())
    return dict(status='extracted',sha256=row['sha256'],bytes=row['bytes'],authority=value['authority'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='operation',required=True)
    for operation in ['inventory','capture']:
        p=sub.add_parser(operation);p.add_argument('repository',type=Path);p.add_argument('destination',type=Path)
    p=sub.add_parser('verify');p.add_argument('archive',type=Path)
    p=sub.add_parser('catalog');p.add_argument('archive',type=Path);p.add_argument('--repository',type=Path)
    p=sub.add_parser('capture-extra');p.add_argument('repository',type=Path);p.add_argument('archive',type=Path);p.add_argument('selection',type=Path)
    p=sub.add_parser('find');p.add_argument('archive',type=Path);p.add_argument('--query',required=True);p.add_argument('--offset',type=int,default=0)
    p=sub.add_parser('extract');p.add_argument('archive',type=Path);p.add_argument('identity');p.add_argument('destination',type=Path)
    args=parser.parse_args()
    if args.operation=='capture':result=capture(args.repository,args.destination)
    elif args.operation=='verify':result=verify(args.archive)
    elif args.operation=='catalog':result=catalog(args.archive,args.repository)
    elif args.operation=='capture-extra':result=capture_extra(args.repository,args.archive,json.loads(args.selection.read_text(encoding='utf-8')))
    elif args.operation=='extract':result=extract(args.archive,args.identity,args.destination)
    elif args.operation=='find':
        if args.offset<0:raise ValueError('invalid offset')
        matches=[r for r in entries(args.archive) if args.query.casefold() in r['path'].casefold()]
        result=dict(items=[dict(r,kind=asset_kind(r['path'])) for r in matches[args.offset:args.offset+20]],
                    next_offset=args.offset+20 if len(matches)>args.offset+20 else None,total=len(matches))
    else:
        result=inventory(args.repository)
        with args.destination.open('x',encoding='utf-8') as out:json.dump(result,out,ensure_ascii=False,indent=2)
        result=dict(entries=len(result['entries']),status='local_inventory_saved',external_upload=False)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
