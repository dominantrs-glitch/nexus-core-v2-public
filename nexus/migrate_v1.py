"""Local non-destructive V1 snapshot. No external delivery or authority promotion."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from nexus.intake import Intake, default_root


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args])


def inventory(repo, ref):
    commit=git(repo,'rev-parse','--verify',ref+'^{commit}').decode().strip()
    entries=[]
    for entry in git(repo,'ls-tree','-r','-l','-z',commit).split(b'\0'):
        if not entry: continue
        info,path=entry.split(b'\t',1); mode,kind,sha,size=info.decode().split()
        path=path.decode('utf-8')
        if any(part in {'','.','..'} or ':' in part or '\\' in part for part in path.split('/')):
            raise ValueError('unsafe repository path')
        if not path.startswith(('brain/projects/','brain/memory/','projects/')): continue
        if mode not in {'100644','100755'} or kind!='blob': continue
        parts=path.split('/')
        copy=(path.startswith(('brain/projects/','brain/memory/')) or
              (len(parts)>2 and parts[2] in {'01_raw','02_web','03_context','07_logs','README.md','AGENTS.md'}))
        entries.append(dict(path=path,blob=sha,bytes=int(size),copy=copy))
    return commit,entries


def write_exact(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=data: raise ValueError('existing snapshot differs')
    else:
        with path.open('xb') as f: f.write(data)


def migrate(repo, workspace, ref='origin/main'):
    repo=Path(repo).resolve()
    commit,entries=inventory(repo,ref)
    root=workspace.root/'legacy'/commit
    root.mkdir(parents=True,exist_ok=True)
    records=[]; copied=0; total=0
    # One persistent git process avoids hundreds of process startups.
    proc=subprocess.Popen(['git','-C',str(repo),'cat-file','--batch'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    try:
        for entry in entries:
            if not entry['copy']: continue
            proc.stdin.write((entry['blob']+'\n').encode()); proc.stdin.flush()
            header=proc.stdout.readline().split()
            if len(header)!=3 or header[1]!=b'blob': raise ValueError('missing Git source')
            data=proc.stdout.read(int(header[2])); terminator=proc.stdout.read(1)
            if len(data)!=entry['bytes'] or terminator!=b'\n': raise ValueError('incomplete Git source')
            digest=hashlib.sha256(data).hexdigest()
            entry['sha256']=digest
            target=root/entry['path']
            if not target.resolve().is_relative_to(root.resolve()):raise ValueError('snapshot boundary')
            write_exact(target,data)
            if hashlib.sha256((root/entry['path']).read_bytes()).hexdigest()!=digest:
                raise ValueError('copy verification failed')
            copied+=1; total+=len(data)
            if entry['path'].startswith('brain/projects/') and entry['path'].endswith('.md'):
                value=data.decode('utf-8-sig')
                if value.startswith('---'):
                    head=value.split('---',2)[1]
                    meta=dict(re.findall(r'^(project|title|status):\s*(.+)$',head,re.M))
                    if 'project' in meta:
                        records.append((meta,entry,value))
    finally:
        proc.stdin.close(); proc.wait(timeout=20); proc.stdout.close()
    mapping=[]
    for meta,entry,value in records:
        source=f'git:ai-workspace@{commit}:{entry["path"]}'
        created=workspace.create(meta.get('title',meta['project']).strip('"\''),source,'v1-project:'+entry['path'])
        project=created['project']
        # Preserve bytes separately; no attempt to reinterpret old DECISION as V2 approval.
        # Stable retry keys support interrupted migrations. Later commits require a new migration scope.
        chunks=[]; chunk=''
        for line in value.splitlines(keepends=True):
            for part in [line[i:i+1000] for i in range(0,len(line),1000)]:
                if len((chunk+part).encode())>7000:
                    chunks.append(chunk);chunk=''
                chunk+=part
        if chunk: chunks.append(chunk)
        for index,chunk in enumerate(chunks):
            # Expected revision is deterministic for this initial import, including retries.
            workspace.save(project,'source',chunk,source,'external_source','',index,
                f'v1-record:{commit}:{entry["path"]}:{index}')
        mapping.append(dict(v1=meta['project'],project=project,source=entry['path'],
                            legacy_status=meta.get('status','unknown'),remote=False))
        workspace.export(project)
    # Preserve existing uncommitted metadata distinctly; never override the remote snapshot.
    overlays=[]
    for raw in git(repo,'diff','--name-only','-z','HEAD','--','brain/projects','brain/memory').split(b'\0'):
        if not raw: continue
        rel=raw.decode(); path=repo/rel
        if path.is_file() and not path.is_symlink():
            data=path.read_bytes(); digest=hashlib.sha256(data).hexdigest()
            write_exact(root/'local-overlays'/digest/rel,data)
            overlays.append(dict(path=rel,sha256=digest,status='uncommitted-not-current-authority'))
    report=dict(source_commit=commit,projects=mapping,copied_files=copied,copied_bytes=total,
        referenced_files=len(entries)-copied,entries=entries,local_overlays=overlays,
        semantics='Exact historical sources; not confirmed V2 Contracts. V1 originals unchanged. No external access granted. Untracked/ignored files and live application environments not migrated.')
    destination=root/'manifest.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(manifest=str(destination),projects=len(mapping),copied_files=copied,copied_bytes=total,
                referenced_files=len(entries)-copied,local_overlays=len(overlays),remote_enabled=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('repository',type=Path)
    p.add_argument('--root',type=Path,default=default_root())
    p.add_argument('--ref',default='origin/main')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    if a.apply: result=migrate(a.repository,Intake(a.root),a.ref)
    else:
        commit,entries=inventory(a.repository,a.ref)
        result=dict(commit=commit,copy_files=sum(x['copy'] for x in entries),copy_bytes=sum(x['bytes'] for x in entries if x['copy']),reference_files=sum(not x['copy'] for x in entries))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
