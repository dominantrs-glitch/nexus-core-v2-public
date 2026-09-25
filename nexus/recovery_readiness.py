"""Read-only verification of a restored package and its activation prerequisites."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from nexus.environment_recovery import safe_file, relative


def inspect(root):
    root=Path(root).resolve(strict=True)
    manifest=json.loads(safe_file(root/'restore-map.json',root).read_text(encoding='utf-8'))
    result=json.loads(safe_file(root/'restore-result.json',root).read_text(encoding='utf-8'))
    if result.get('status')!='isolated_restore_verified' or result.get('services_started') is not False:
        raise ValueError('isolated verified restoration required')
    checked=0
    for r in manifest['records']:
        path=safe_file(root.joinpath(*relative(r['path']).parts),root)
        with path.open('rb') as source:
            actual=hashlib.file_digest(source,'sha256').hexdigest()
        if path.stat().st_size!=r['bytes'] or actual!=r['sha256']:
            raise ValueError('restored file changed; repeat verification before activation')
        checked+=1
    tasks=[]
    for path in (root/'roots/activation').glob('*.xml'):
        doc=ET.fromstring(safe_file(path,root).read_text(encoding='utf-8-sig'))
        ns={'s':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        actions=doc.findall('.//s:Actions/s:Exec',ns)
        if not actions or any(not a.findtext('s:Command',namespaces=ns) for a in actions):
            raise ValueError('scheduled task has no recoverable action')
        tasks.append(dict(name=path.stem,actions=len(actions),activation='requires runtime installation and path review'))
    return dict(status='data_verified_activation_pending',files=checked,scheduled_tasks=tasks,
                services_started=False,credentials_exposed=False,other_pc_tested=False,
                credential_portability=manifest['credential_portability'],
                remaining=['Store encrypted package and separate key off this PC.',
                           'Install pinned runtimes, relocate paths and reauthorize Windows-bound credentials.',
                           'Confirm stopping old writers before final snapshot and cutover.',
                           'Verify fresh read/write and owner sign-in before enabling scheduled jobs once.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('restored',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=inspect(args.restored)
    if args.output:args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
