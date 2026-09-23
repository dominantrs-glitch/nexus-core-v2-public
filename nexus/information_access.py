"""Owner-local information coverage inventory; no upload or implicit migration.

Compare immutable Git metadata with one read-only intake transaction. Do not read
archived routed notes as current, infer completion, or treat a reference as access.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import quote

from nexus.intake import default_root


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE)


def tree(repo, ref):
    if ref.startswith('-'):
        raise ValueError('invalid Git ref')
    commit = git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    result = {}
    for entry in git(repo, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        info, path = entry.split(b'\t', 1)
        mode, kind, blob = info.decode().split()
        if kind == 'blob' and mode in {'100644', '100755'}:
            result[path.decode('utf-8')] = blob
    return commit, result


def header(repo, blob):
    data = git(repo, 'cat-file', 'blob', blob).decode('utf-8-sig')
    if not data.startswith('---\n') and not data.startswith('---\r\n'):
        return {}
    parts = data.split('---', 2)
    if len(parts) != 3:
        return {}
    # Only scalar discovery metadata; never interpret a document as instructions.
    return dict(re.findall(r'^(project|title|status|last_confirmed):[ \t]*(.+)$', parts[1], re.M))


def inventory(root, repository, ref='origin/main', important=()):
    root, repository = Path(root).resolve(), Path(repository).resolve()
    database = (root / 'intake.sqlite3').resolve(strict=True)
    commit, blobs = tree(repository, ref)
    project_paths = {p: b for p, b in blobs.items()
                     if p.startswith('brain/projects/') and p.endswith('.md')}
    metadata = {p: {k: v.strip().strip('\"\'') for k, v in header(repository, b).items()}
                for p, b in project_paths.items()}
    dirty = {p.decode() for p in git(repository, 'diff', '--name-only', '-z', 'HEAD', '--',
                                   'brain/projects').split(b'\0') if p}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        projects = [dict(p) for p in db.execute('SELECT * FROM projects ORDER BY id')]
        routes = {r['project'] for r in db.execute('SELECT project FROM intake_routes')}
        fences = {r['project'] for r in db.execute('SELECT project FROM intake_fences')}
        # No bodies, quotes, request IDs, owner receipts or credentials enter this report.
        sources = {}
        for n in db.execute("SELECT project,source,(kind='proposal' AND evidence='model_inference' "
                            "AND substr(body,1,9)='【画面用の概要】' || char(10)) AS overview "
                            "FROM notes ORDER BY project,revision"):
            sources.setdefault(n['project'], []).append((n['source'], bool(n['overview'])))
    important = set(important)
    known = {p['id'] for p in projects}
    if not important <= known:
        raise ValueError('important project selection unavailable')
    baseline_trees, covered, items = {}, set(), []
    for p in projects:
        match = re.fullmatch(r'git:ai-workspace@([a-f0-9]{40}):(brain/projects/[^\r\n]+\.md)', p['source'])
        legacy = None
        gaps = []
        if match:
            baseline, path = match.groups()
            covered.add(path)
            if baseline not in baseline_trees:
                try:
                    baseline_trees[baseline] = tree(repository, baseline)[1]
                except subprocess.CalledProcessError:
                    baseline_trees[baseline] = None
            old = baseline_trees[baseline]
            freshness = ('missing' if path not in blobs else 'baseline_unavailable' if old is None or path not in old
                         else 'unchanged' if old[path] == blobs[path] else 'changed_since_import')
            legacy = dict(path=path, commit=commit, blob=blobs.get(path), import_commit=baseline,
                          comparison=freshness, worktree_modified=path in dirty,
                          metadata=metadata.get(path, {}),
                          url='https://github.com/YOUR_GITHUB_ACCOUNT/ai-workspace/blob/' + commit + '/' + quote(path, safe='/'),
                          retrieval='local_git_verified_remote_client_unverified')
            if freshness != 'unchanged':
                gaps.append('legacy_' + freshness)
            if path in dirty:
                gaps.append('uncommitted_legacy_record')
        else:
            gaps.append('no_legacy_project_mapping')
        routed = p['id'] in routes
        frozen = p['id'] in fences
        additions = None if routed else sum(not s.startswith('git:ai-workspace@') and not overview
                                            for s, overview in sources.get(p['id'], []))
        if not routed:
            gaps.append('not_in_git_common_entry')
            if additions:
                gaps.append('local_post_import_records')
        if frozen and not routed:
            gaps.append('frozen_without_route')
        status = (legacy or {}).get('metadata', {}).get('status', 'UNKNOWN')
        basis = ('owner_selected' if p['id'] in important else 'legacy_active' if status == 'ACTIVE'
                 else 'local_updates' if additions else 'legacy_waiting' if status == 'WAITING' else 'unclassified')
        items.append(dict(project=p['id'], title=p['title'],
                          canonical='git_route' if routed else 'frozen_local' if frozen else 'local',
                          revision=None if routed else p['revision'],
                          cloud_status='registered_not_live_verified' if routed else 'not_registered',
                          sharing_enabled=bool(p['remote']), legacy=legacy,
                          local_nonlegacy_note_count=additions, priority_basis=basis, gaps=gaps))
    order = {'owner_selected': 0, 'legacy_active': 1, 'local_updates': 2, 'legacy_waiting': 3, 'unclassified': 4}
    items.sort(key=lambda p: (order[p['priority_basis']], p['project']))
    unmatched = [dict(path=p, blob=project_paths[p], metadata=metadata[p])
                 for p in sorted(project_paths.keys() - covered)]
    scopes = {
        'shared_rules': ['brain/ai-operating-principles.md', 'brain/AGENTS.md'],
        'personal_judgments_and_learning': [p for p in blobs if p.startswith('brain/memory/')],
        'source_materials': [p for p in blobs if p.startswith('projects/') and '/01_raw/' in p],
        'deliverables': [p for p in blobs if p.startswith('projects/') and '/05_output/' in p],
    }
    result = dict(schema=1, mode='owner-local-read-only', legacy_commit=commit,
                  legacy_ref=ref, ref_freshness='caller_must_fetch_or_verify_remote',
                  projects=items, unmatched_legacy_projects=unmatched,
                  information_groups={k: dict(tracked_files=sum(p in blobs for p in paths),
                      coverage='not_assessed', migration='not_authorized_by_inventory') for k, paths in scopes.items()},
                  summary=dict(intake_projects=len(items), legacy_project_files=len(project_paths),
                      routed=sum(p['canonical'] == 'git_route' for p in items),
                      local_only=sum(p['canonical'] != 'git_route' for p in items)),
                  limitations=['Registration is not cloud or phone availability.',
                      'Counts and ACTIVE status do not establish importance, completion, sensitivity or permission.',
                      'Legacy sources are historical evidence, not current owner confirmation.',
                      'Routed archive revisions and notes are omitted; read the canonical route.',
                      'Ignored/untracked files, local-only databases and external services need separate scoped checks.'])
    result['inventory_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_root())
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--important', action='append', default=[])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = inventory(args.root, args.repository, args.ref, args.important)
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(data + '\n')
        print(json.dumps(dict(output=str(args.output), **result['summary']), ensure_ascii=False))
    else:
        print(data)


if __name__ == '__main__':
    main()
