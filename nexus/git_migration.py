"""Read-only intake migration inventory. No upload, cutover, deletion or approval.

The report is owner-local and contains hashes/metadata, not note bodies or quotes.
Staging is an explicit separate operation into a NEW local directory. It preserves
all note versions; it does not migrate owner receipts, Task, Personal or Learning.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from nexus.intake import default_root, identifier, KINDS, OVERVIEW_HEADER


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def snapshot(root, projects):
    """A single SQLite read transaction, opened with mode=ro (no schema creation)."""
    dbpath = (Path(root) / 'intake.sqlite3').resolve(strict=True)
    if not projects or len(set(projects)) != len(projects):
        raise ValueError('explicit unique project selection required')
    for project in sorted(projects):
        identifier(project)
    with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        return snapshot_db(db, projects)


def snapshot_db(db, projects):
    """Caller owns the read/write transaction; no separate connection or snapshot."""
    selected = []
    receipts = [dict(r) | {'result': json.loads(r['result'])} for r in db.execute('SELECT * FROM requests ORDER BY key')]
    for project in sorted(projects):
        row = db.execute('SELECT * FROM projects WHERE id=?', (project,)).fetchone()
        if row is None:
            raise ValueError('project unavailable')
        notes = [dict(n) for n in db.execute('SELECT * FROM notes WHERE project=? ORDER BY revision', (project,))]
        selected.append(dict(project=dict(row), notes=notes,
            requests=[r for r in receipts if r['result'].get('project') == project]))
    return selected


def inspect(selected):
    reports, blockers = [], []
    for item in selected:
        p, notes = item['project'], item['notes']
        identifier(p['id'])
        by_id = {n['id']: n for n in notes}
        replaced, effective, rows = set(), {}, []
        previous_revision = 0
        for n in notes:
            issues = []
            try:
                identifier(n['id'])
            except ValueError:
                issues.append('invalid_identity')
            if n['revision'] != previous_revision + 1:
                issues.append('revision_gap_or_duplicate')
            if n['project'] != p['id']:
                issues.append('wrong_project')
            previous_revision = n['revision']
            target = by_id.get(n['supersedes'])
            if n['supersedes']:
                if target is None or target['revision'] >= n['revision'] or n['supersedes'] in replaced:
                    issues.append('broken_or_duplicate_supersession')
                else:
                    inherited = effective.get(target['id'], target['kind'])
                    if n['kind'] not in {inherited, 'correction'}:
                        issues.append('correction_category_mismatch')
                    if (inherited in {'constraint', 'explicit_choice', 'preference'} or
                            target['evidence'] == 'user_statement') and n['evidence'] != 'user_statement':
                        issues.append('authority_change')
                replaced.add(n['supersedes'])
            effective[n['id']] = effective.get(n['supersedes'], n['kind']) if n['kind'] == 'correction' else n['kind']
            if n['kind'] not in KINDS or n['evidence'] not in {'user_statement','model_inference','external_source'}:
                issues.append('unknown_classification')
            if n['evidence'] == 'user_statement' and not n['quote'].strip():
                issues.append('missing_user_quote')
            if n['evidence'] != 'user_statement' and n['quote']:
                issues.append('misattributed_quote')
            if effective[n['id']] in {'constraint','explicit_choice','preference'} and n['evidence'] != 'user_statement':
                issues.append('inferred_binding_category')
            # Deliberately conservative signal, NOT a claim that unflagged text is safe.
            suspected = bool(re.search(r'-----BEGIN .*PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
                                       n['body'] + '\n' + n['source'] + '\n' + n['quote']))
            if suspected:
                issues.append('possible_secret_review_required')
            rows.append(dict(id=n['id'], revision=n['revision'], kind=effective[n['id']], captured_kind=n['kind'],
                evidence=n['evidence'], authority='attributed-input-not-owner-confirmation', binding=False,
                source_sha256=digest(n['source']), record_sha256=digest(n), created=n['created'],
                supersedes=n['supersedes'], issues=issues))
        if previous_revision != p['revision']:
            blockers.append(dict(project=p['id'], issue='project_revision_mismatch'))
        for receipt in item.get('requests', []):
            try:
                receipt_input(item, receipt)
            except (ValueError, KeyError, TypeError):
                blockers.append(dict(project=p['id'], issue='invalid_request_receipt'))
        if 'requests' in item:
            covered = {r['result'].get('note') for r in item['requests']}
            if None not in covered or any(n['id'] not in covered for n in notes):
                blockers.append(dict(project=p['id'], issue='missing_request_receipt'))
        for row in rows:
            row['lifecycle'] = 'historical' if row['id'] in replaced else 'current'
            row['disposition'] = 'review' if row['issues'] else 'preserve'
            blockers.extend(dict(project=p['id'], note=row['id'], issue=issue) for issue in row['issues'])
        current = [n for n in notes if n['id'] not in replaced]
        overview = next((n for n in reversed(current) if effective[n['id']] == 'proposal' and
                         n['evidence'] == 'model_inference' and n['body'].startswith(OVERVIEW_HEADER)), None)
        reports.append(dict(project=p['id'], revision=p['revision'], remote=bool(p['remote']),
            source_sha256=digest(item), notes=rows, current_count=len(current), history_count=len(notes)-len(current),
            request_count=len(item.get('requests', [])),
            overview=None if overview is None else dict(note=overview['id'], current=overview['revision']==p['revision']),
            duplicate_content_groups=[ids for ids in _duplicates(notes).values() if len(ids)>1]))
    return dict(schema=1, mode='read-only-dry-run', source_snapshot_sha256=digest(selected), projects=reports,
        blockers=blockers, migration_ready=False,
        remaining=['Destination/data-scope approval and sensitivity review',
            'Task/Contract/Decision, global rules, personal/learning dependencies and original assets are NOT in this intake-only inventory',
            'Freeze ALL old writes, export again, compare snapshot and preserve request receipts before any real cutover',
            'Cloud client E2E and independent restore required; no old data is deleted'])


def _duplicates(notes):
    groups = {}
    for n in notes:
        groups.setdefault(digest(n['body']), []).append(n['id'])
    return groups  # Equal text still retains separate source, time and identity.


def receipt_input(item, receipt):
    """Recover only payloads whose original Python digest verifies exactly.

    These are draft idempotency receipts, NEVER native owner confirmations.
    The old remote flag is verified but not exported as a grant: destination ACL
    is checked independently on every replay. Raw request IDs stay owner-local.
    """
    p, result = item['project'], receipt['result']
    required = {'project','revision','status','binding'} | ({'note'} if 'note' in result else set())
    if set(result) != required or type(result.get('revision')) is not int:
        raise ValueError('invalid request receipt')
    if (result.get('project') != p['id'] or result.get('status') != 'saved-draft'
            or result.get('binding') is not False):
        raise ValueError('invalid request receipt')
    if 'note' in result:
        n = next((n for n in item['notes'] if n['id'] == result['note']), None)
        if n is None or result['revision'] != n['revision']:
            raise ValueError('invalid request receipt')
        operation = 'save'
        args = {k:n[k] for k in ('project','kind','body','source','evidence','quote','supersedes')}
        args['expected_revision'] = n['revision'] - 1
        payload = ['save', n['project'], n['kind'], n['body'], n['source'], n['evidence'],
                   n['quote'], n['revision'] - 1, n['supersedes']]
    else:
        if result['revision'] != 0:
            raise ValueError('invalid request receipt')
        operation, args = 'create', {k:p[k] for k in ('title','source')}
        payload = ['create', p['title'], p['source']]
    if receipt['digest'] not in {hashlib.sha256(json.dumps(payload+[remote], ensure_ascii=False,
            sort_keys=True).encode()).hexdigest() for remote in (False, True)}:
        raise ValueError('request receipt digest mismatch')
    return operation, args


def receipt_path(owner, key):
    # Match JSON.stringify([owner, request_id]); no object-key ordering involved.
    value = json.dumps([owner, key], ensure_ascii=False, separators=(',', ':')).encode()
    return 'legacy-requests/' + hashlib.sha256(value).hexdigest() + '.json'


def stage(selected, owner, target):
    """Local review fixture only. Production routing is intentionally unsupported."""
    report = inspect(selected)
    if report['blockers']:
        raise ValueError('migration audit has blockers')
    if not isinstance(owner, str) or not owner.strip() or len(owner.encode()) > 200:
        raise ValueError('explicit owner required')
    target = Path(target).resolve()
    if target.exists():
        raise ValueError('staging destination must not exist')
    files = {}
    catalog = dict(schema=1, mode='migration-review', owner=owner, generation='migration-'+report['source_snapshot_sha256'][:24],
                   imported_receipts=1, projects=[])
    for item, audit in zip(selected, report['projects']):
        p, notes = item['project'], item['notes']
        current = [n['id'] for n in audit['notes'] if n['lifecycle']=='current']
        catalog['projects'].append({k:p[k] for k in ('id','title','revision')} | {'remote':bool(p['remote']), 'write_state':'frozen'})
        prefix = 'projects/'+p['id']
        files[prefix+'/manifest.json'] = dict(id=p['id'], title=p['title'], revision=p['revision'], source=p['source'],
            remote=bool(p['remote']), write_state='frozen', current=current, overview=audit['overview']['note'] if audit['overview'] else None)
        for n, classification in zip(notes, audit['notes']):
            files[prefix+'/records/'+n['id']+'.json'] = n | {'kind':classification['kind'], 'captured_kind':n['kind']}
        for receipt in item.get('requests', []):
            operation, args = receipt_input(item, receipt)
            files[receipt_path(owner, receipt['key'])] = dict(schema=1, owner=owner, generation=catalog['generation'],
                operation=operation, input=args, source_digest=receipt['digest'], result=receipt['result'])
    files['nexus.json'] = catalog
    # Explicitly outside the canonical schema: never claim production migration.
    files['MIGRATION-REVIEW.json'] = dict(report=report, production_cutover_allowed=False,
        request_receipts_migrated=True, native_owner_receipts_included=False)
    target.mkdir(parents=True)
    for name, value in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as f:
            f.write(encoded(value))
    return dict(path=str(target), files=len(files), source_snapshot_sha256=report['source_snapshot_sha256'],
                production_cutover_allowed=False)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_root())
    parser.add_argument('--project', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage-owner', help='Explicit local staging instead of metadata-only audit; output must be new')
    a = parser.parse_args()
    selected = snapshot(a.root, a.project)
    if a.stage_owner:
        result = stage(selected, a.stage_owner, a.output)
    else:
        report = inspect(selected)
        a.output.parent.mkdir(parents=True, exist_ok=True)
        with a.output.open('xb') as f:
            f.write(encoded(report))
        result = dict(path=str(a.output), projects=len(report['projects']), blockers=len(report['blockers']), migration_ready=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
