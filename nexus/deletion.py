"""Owner-local reviewed deletion of current shared data, retaining Git history.

Never infer a real target from a general cleanup request. Full history erasure
needs a separate explicit repository/copy-retention scope. Confirmation is the
final step before writing; no native Contract or User UAT is inferred.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from nexus.owner import OwnerCapability, ApprovalLedger, ApprovalRequest, WindowsConfirmationSurface


def run(payload):
    base = Path(__file__).resolve().parents[1]
    runner = base / 'cloudflare/dist/git-deletion-local.mjs'
    sources = list((base / 'cloudflare/src').glob('git-*.ts'))
    if not runner.is_file() or any(p.stat().st_mtime > runner.stat().st_mtime for p in sources):
        raise ValueError('rebuild owner-local deletion runner before use')
    node = shutil.which('node')
    if not node:
        raise ValueError('local Node runtime unavailable')
    response = subprocess.run([node, str(runner)], input=json.dumps(payload, ensure_ascii=False),
                              encoding='utf-8', capture_output=True, timeout=120)
    value = json.loads(response.stdout)
    if response.returncode or not value.get('ok'):
        raise ValueError('deletion_unavailable_or_outcome_unknown')
    return value['result']


def prepare(repository, ref, request, output):
    view = run(dict(operation='plan', repository=str(repository), ref=ref, input=request))
    package = dict(repository=str(Path(repository).resolve()), ref=view['snapshot'], view=view)
    with Path(output).open('x', encoding='utf-8') as target:
        json.dump(package, target, ensure_ascii=False, indent=2)
    return view


def apply(preview_file, config, owner_root, owner):
    package = json.loads(Path(preview_file).read_text(encoding='utf-8'))
    saved = package['view']
    fresh = run(dict(operation='plan', repository=package['repository'], ref=package['ref'], input=saved['input']))
    if fresh != saved or fresh['blockers']:
        raise ValueError('deletion dependencies or reviewed plan changed')
    root = Path(owner_root)
    capability = OwnerCapability.open(root / 'owner.capability.json', owner)
    capability.authenticate(owner)
    impact = fresh['impact']
    summary = (f"「{impact['title']}」を現在の共通保存先から削除します。\n"
               f"現在の記録: {impact['current_notes']}件、作業: {impact['work_items']}件、対象ファイル: {impact['removed_files']}件。\n"
               f"他案件に残る言及: {impact['other_current_text_references']}件。\n"
               "過去のGit履歴、バックアップ、他案件の記述は残ります。全履歴の消去ではありません。\n"
               "この具体的な範囲を削除してよい場合だけOKを押してください。\n"
               f"確認番号: {fresh['plan_digest']}")
    request = ApprovalRequest('current_shared_data_deletion', fresh['input']['project'], impact['revision'], summary)
    if not WindowsConfirmationSurface().confirm(request):
        return dict(status='cancelled', data_changed=False)
    receipt = ApprovalLedger(root / 'approvals.sqlite3').record(owner, capability, request)
    result = run(dict(operation='apply', repository=package['repository'], ref=package['ref'], input=saved['input'],
                      plan_digest=fresh['plan_digest'], config=str(config), approval_receipt=receipt))
    result['approval_receipt'] = receipt
    Path(str(preview_file) + '.result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('repository', type=Path)
    plan.add_argument('ref')
    plan.add_argument('input', type=Path)
    plan.add_argument('output', type=Path)
    execute = sub.add_parser('apply')
    execute.add_argument('preview', type=Path)
    execute.add_argument('--config', required=True, type=Path)
    execute.add_argument('--owner-root', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'NexusCoreV2/owner')
    execute.add_argument('--owner', required=True)
    a = parser.parse_args()
    try:
        result = prepare(a.repository, a.ref, json.loads(a.input.read_text(encoding='utf-8')), a.output) if a.operation == 'plan' else apply(a.preview, a.config, a.owner_root, a.owner)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(json.dumps(dict(status='deletion_unavailable_or_outcome_unknown',
                              instruction='Read current state and review again; never repeat a deletion blindly.')))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
