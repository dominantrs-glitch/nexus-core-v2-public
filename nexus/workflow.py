"""Resumable, local synthetic vertical slice. No server or external writes.

Run python -m nexus.workflow --help. This is an operator path, never an MCP
authority endpoint. The owner surface is injectable only for isolated tests.
"""
import argparse
import base64
import getpass
import hashlib
from html import escape
import json
import os
from pathlib import Path
import tempfile

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext, LocalBinaryProvider
from nexus.context_store import ContextStore
from nexus.core_backup import export_core, restore_core
from nexus.owner import ApprovalDeclined, open_local_owner_adapter
from nexus.project import ProjectStore

PROJECT = 'synthetic-card'
ORIGINAL_SHA = '8cc35def2b5017783ddb06748555ca36c212c8ff4f6343f96c05224fdcc76df0'
TITLE = '原本確認カード'
GOAL = '合成画像の同じ原本を載せた、日本語の確認用HTMLカードを制作・修正し、AI間の受渡しと復元を検証する。'
ACCEPTANCE = {'card': ['machine', 'contract', 'uat'], 'same-original': ['machine', 'contract'],
              'rework': ['machine', 'contract'], 'portable': ['machine', 'contract']}
CONSTRAINTS = ['合成データだけを使う。課金・新資格情報・サービス常設は行わない。',
               '原本は改変・再生成せず、固定版とSHA-256で照合する。',
               '本人UAT・AI観察・機械検証を分け、欠落や権限不足ならDONEにしない。']
DECISION = 'この合成案件では原本画像を変更せず、必要な文脈だけを添付して制作・修正・検証する。'
OPERATIONS = frozenset({'implement', 'handoff', 'finish'})


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = json.dumps(value, ensure_ascii=False, indent=2)
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
        os.replace(staged, path)
    finally:
        Path(staged).unlink(missing_ok=True)


class Workflow:
    def __init__(self, root: Path, owner: str, *, owner_root=None, surface=None):
        self.root, self.owner = root.resolve(), owner
        self.root.mkdir(parents=True, exist_ok=True)
        self.contexts = ContextStore(self.root / 'context.sqlite3')
        self.artifacts = ArtifactStore(self.root / 'artifacts.sqlite3', LocalBinaryProvider(self.root / 'blobs'))
        self.projects = ProjectStore(self.root / 'projects.sqlite3', context_store=self.contexts, artifact_store=self.artifacts)
        self.owner_root, self.surface = owner_root, surface

    def adapter(self):
        return open_local_owner_adapter(self.owner, self.projects, self.contexts,
                                        data_root=self.owner_root, surface=self.surface)

    def snapshot(self):
        try:
            return self.projects.export(PROJECT, self.owner)
        except AccessDenied:
            # Only an absent project is an unstarted workflow; another owner is denied.
            with self.projects._db() as db:
                if db.execute('SELECT 1 FROM projects WHERE id=?', (PROJECT,)).fetchone():
                    raise
            return None

    def _source(self, identity, body):
        snapshot = self.contexts.export(PROJECT, self.owner)
        matches = [s for s in snapshot['sources'] if s['id'] == identity]
        if matches:
            if matches[-1]['body'] != body:
                raise ValueError('source conflict')
            from nexus.context_store import SourceRef
            return SourceRef(identity, matches[-1]['revision'], matches[-1]['sha256'])
        return self.contexts.register_source(identity, PROJECT, self.owner, body)

    def prepare(self):
        fixture = Path(__file__).resolve().parent / 'data' / 'synthetic-original.png'
        data = fixture.read_bytes()
        if len(data) != 475 or hashlib.sha256(data).hexdigest() != ORIGINAL_SHA:
            raise InsufficientContext('synthetic fixture changed')
        self._register('original', data, 'image/png', 1)
        self._register('reference', ('Title: ' + TITLE + '\n' + GOAL).encode(), 'text/plain', 1)
        draft = dict(project=PROJECT, goal=GOAL, acceptance=ACCEPTANCE, constraints=CONSTRAINTS,
                     decision=DECISION, authority='draft', original_sha256=ORIGINAL_SHA)
        dump(self.root / 'review' / 'contract-draft.json', draft)
        original = self.root / 'review' / 'original.png'
        original.write_bytes(data)
        (self.root / 'review' / 'start.html').write_text(
            '<!doctype html><meta charset="utf-8"><title>Nexus 合成案件</title>'
            '<style>body{max-width:850px;margin:40px auto;font:17px/1.8 system-ui}pre{white-space:pre-wrap}</style>'
            '<h1>確認カードの合成案件</h1><p>これは確定前の提案です。次の本人確認で、Contractとこの案件だけのDecisionを確認します。</p>'
            '<img src="original.png" alt="合成原本"><pre>' + escape(json.dumps(draft, ensure_ascii=False, indent=2)) + '</pre>', encoding='utf-8')
        return self.status()

    def _register(self, identity, data, mime, revision):
        try:
            old = self.artifacts.read(identity, revision, Access(self.owner, PROJECT)).data
        except (InsufficientContext, AccessDenied):
            # Registration enforces existing scope/readers and exact previous revision.
            return self.artifacts.register(identity, PROJECT, data, mime, (self.owner,), expected_revision=revision-1)
        if old != data:
            raise ValueError('immutable artifact conflict')
        return revision

    def start(self):
        self.prepare()
        adapter = self.adapter()
        snapshot = self.snapshot()
        if snapshot is None:
            adapter.confirm_contract(PROJECT, self.owner, goal=GOAL, acceptance=ACCEPTANCE,
                                     constraints=CONSTRAINTS, source='synthetic-workflow-v1')
        elif snapshot['contracts'][-1]['goal'] != GOAL or snapshot['contracts'][-1]['acceptance'] != ACCEPTANCE:
            raise ValueError('workflow contract differs')
        entries = self.contexts.export(PROJECT, self.owner)['contexts']
        decision = [c for c in entries if c['id'] == 'slice-decision']
        if not decision:
            adapter.confirm_decision_from_body('slice-decision', PROJECT, self.owner,
                source_id='slice-decision-source', source_body=DECISION, body=DECISION,
                projects=frozenset({PROJECT}), operations=OPERATIONS, readers=frozenset({self.owner}))
        elif decision[-1]['body'] != DECISION or not decision[-1]['approval_receipt']:
            raise ValueError('workflow decision differs')
        source = self._source('synthetic-fixture', 'Synthetic context fixture; not the owner personal profile.')
        fixtures = [
            ('personal', 'save_personal_context', 'この合成案件では説明を短い日本語にする。', OPERATIONS, {self.owner}, 'current'),
            ('old-personal', 'save_personal_context', '歴史的な合成設定。', OPERATIONS, {self.owner}, 'historical'),
            ('unrelated', 'save_personal_context', 'UNRELATED_FIXTURE_DO_NOT_DELIVER', {'other-operation'}, {self.owner}, 'current'),
            ('restricted', 'save_personal_context', 'RESTRICTED_FIXTURE_DO_NOT_DELIVER', OPERATIONS, {'other-reader'}, 'current'),
            ('proposal', 'derive', '余白を十分に取るというAI提案。必須条件ではない。', OPERATIONS, {self.owner}, 'current'),
        ]
        ids = {c['id'] for c in self.contexts.export(PROJECT, self.owner)['contexts']}
        for identity, method, body, operations, readers, status in fixtures:
            if identity not in ids:
                getattr(self.contexts, method)(identity, PROJECT, self.owner, source=source, body=body,
                    projects=frozenset({PROJECT}), operations=frozenset(operations), readers=frozenset(readers), status=status)
        for operation in OPERATIONS:
            try:
                self.contexts.policy(PROJECT, self.owner, 1, operation)
            except InsufficientContext:
                self.contexts.set_operation_policy(PROJECT, self.owner, contract_revision=1, operation=operation,
                    principal=self.owner, required_context=frozenset({'slice-decision', 'personal'}),
                    required_authority={'slice-decision': 'confirmed'}, required_artifacts=(('original', 1), ('reference', 1)))
        return self.status()

    def resolve(self, operation):
        try:
            policy = self.contexts.policy(PROJECT, self.owner, 1, operation)
            result = self.contexts.resolve(PROJECT, self.owner, operation, required=policy.required_context,
                                            required_authority=policy.required_authority)
            for identity, revision in policy.required_artifacts:
                with self.artifacts.open_verified(identity, revision, Access(self.owner, PROJECT)):
                    pass
            return result
        except (InsufficientContext, AccessDenied):
            if self.snapshot() is not None:
                with self.projects._db() as db:
                    self.projects._event(db, PROJECT, 1, 'context_gate',
                                         {'operation': operation, 'result': 'insufficient_context'})
                    db.execute("UPDATE projects SET state='blocked' WHERE id=?", (PROJECT,))
            raise InsufficientContext('required operation context or original unavailable') from None

    def output(self):
        snapshot = self.snapshot()
        if snapshot is None:
            raise InsufficientContext('confirmed contract required')
        return next((e for e in reversed(snapshot['events']) if e['kind'] == 'output'), None)

    def build(self, *, correction=None, note=None, source='local-operator-correction'):
        self.adapter().capability.authenticate(self.owner)
        self.resolve('implement')
        output = self.output()
        if output and correction is None:
            return self.verify()
        if correction is not None and (not isinstance(correction, str) or not correction.strip()):
            raise ValueError('correction required')
        if note is not None and (not correction or not isinstance(note, str) or not note.strip()):
            raise ValueError('note requires a correction')
        events = self.snapshot()['events']
        # Resume the same pending correction instead of creating duplicate output revisions.
        previous = next((e for e in reversed(events) if e['kind'] == 'correction'), None)
        if correction and previous and previous['payload']['body'] == correction and output and output['seq'] > previous['seq']:
            if note and escape(note) not in self.artifacts.read('card', output['payload']['revision'], Access(self.owner,PROJECT)).data.decode('utf-8'):
                raise ValueError('existing correction has a different note; record a new correction')
            return self.verify()
        if correction and output and not (previous and previous['seq'] > output['seq'] and previous['payload']['body'] == correction):
            self.projects.correction(PROJECT, self.owner, expected_revision=1, output_event=output['seq'],
                acceptance='card', body=correction, source=source)
        revision = output['payload']['revision'] + 1 if output else 1
        original = self.artifacts.read('original', 1, Access(self.owner, PROJECT))
        # A compact fixture renderer: correction text is faithfully included, never treated as code.
        html = ('<!doctype html><html lang="ja"><meta charset="utf-8"><title>' + TITLE + '</title>'
            '<style>body{max-width:840px;margin:40px auto;padding:20px;font:18px/1.8 system-ui;'
            'background:#f4f6f9;color:#18283b}main{padding:32px;background:white;border-radius:16px}'
            'img{width:100%;image-rendering:pixelated}code{overflow-wrap:anywhere}</style><main><h1>' + TITLE + '</h1>'
            '<p>これは架空案件の確認カードです。</p><img alt="合成原本" src="data:image/png;base64,' +
            base64.b64encode(original.data).decode() + '"><p>原本 revision 1 / 475 bytes</p><code>' +
            original.sha256 + '</code>' + (('<h2>検証範囲</h2><p>' + escape(note) +
                '</p><details><summary>今回の修正依頼</summary><p>' + escape(correction) + '</p></details>') if note else
                ('<h2>修正内容</h2><p>' + escape(correction) + '</p>' if correction else '')) +
            '<p>成果物 revision ' + str(revision) + '</p></main></html>')
        self._register('card', html.encode(), 'text/plain', revision)
        manifest = self.artifacts.describe('card', revision, Access(self.owner, PROJECT))
        self.adapter().bind_output(PROJECT, self.owner, expected_revision=1, artifact_id='card',
                                   artifact_revision=revision, sha256=manifest['sha256'])
        (self.root / 'review' / 'card.html').write_text(html, encoding='utf-8')
        return self.verify()

    def _record(self, acceptance, kind, status, source):
        output = self.output()
        if not output:
            raise InsufficientContext('output required')
        for e in reversed(self.snapshot()['events']):
            p = e['payload']
            if e['kind'] == 'verification' and (p['output_event'], p['acceptance'], p['kind']) == (output['seq'], acceptance, kind):
                if p['status'] == status and p['source'] == source:
                    return e['seq']
                break
        return self.adapter().record_verification(PROJECT, self.owner, expected_revision=1, output_event=output['seq'],
             acceptance=acceptance, kind=kind, status=status, source=source)

    def verify(self):
        import re
        output = self.output()
        if not output:
            raise InsufficientContext('output required')
        self.resolve('finish')
        raw = self.artifacts.read('card', output['payload']['revision'], Access(self.owner, PROJECT)).data
        html = raw.decode('utf-8')
        encoded = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', html)
        if not encoded or hashlib.sha256(base64.b64decode(encoded[1], validate=True)).hexdigest() != ORIGINAL_SHA:
            raise InsufficientContext('card original mismatch')
        if '<h1>' + TITLE + '</h1>' not in html:
            raise InsufficientContext('card title mismatch')
        self._record('card', 'machine', 'pass', 'local:HTML UTF-8/title/embedded-original SHA-256')
        self._record('card', 'contract', 'pass', 'local:bounded synthetic card title and immutable original match')
        self._record('same-original', 'machine', 'pass', 'local:exact source bytes and embedded original SHA-256')
        corrections = [e for e in self.snapshot()['events'] if e['kind'] == 'correction']
        if corrections and output['seq'] > corrections[-1]['seq']:
            body = corrections[-1]['payload']['body']
            if escape(body) not in html:
                raise InsufficientContext('correction omitted')
            for kind in ('machine', 'contract'):
                self._record('rework', kind, 'pass', 'local:correction bound to previous output and included in revised artifact')
            if not any(e['kind'] == 'candidate' for e in self.snapshot()['events']):
                self.projects.candidate(PROJECT, self.owner, evidence_events=[corrections[-1]['seq']],
                    body='候補: 原本を変えない見せ方の修正でも、成果物の版を更新して再検証する。適用はこの合成案件内。')
        return self.status()

    def handoff(self):
        resolution = self.resolve('handoff')
        destination = self.root / 'handoff'
        destination.mkdir(exist_ok=True)
        original = self.artifacts.read('original', 1, Access(self.owner, PROJECT))
        reference = self.artifacts.read('reference', 1, Access(self.owner, PROJECT))
        (destination / 'original.png').write_bytes(original.data)
        (destination / 'reference.txt').write_bytes(reference.data)
        manifest = original.manifest()
        # Selected content only; exclude owner account, capabilities, approval IDs and full ledgers.
        selected = [dict(id=i.record.id, revision=i.record.revision, kind=i.record.kind,
                         authority=i.record.authority, binding=i.binding, body=i.record.body,
                         source_id=i.record.source_id, source_revision=i.record.source_revision) for i in resolution.items]
        dump(destination / 'context.json', {'project': PROJECT, 'contract': dict(goal=GOAL, acceptance=ACCEPTANCE, constraints=CONSTRAINTS),
                                         'context': selected, 'original': manifest, 'reference': reference.manifest()})
        (destination / 'request.txt').write_text(
            '添付はNexusの合成案件です。context.jsonは案件の確定条件と選別済みContext、original.pngは原本です。\n'
            '1. 原本を直接見て配置と色を説明してください。説明の推測や画像再生成はしないでください。\n'
            '2. 実際の添付ファイルをコードで読み、byte数、SHA-256、厳密PNG読込みの幅と高さを出してください。'
            'metadataの転記・Base64再入力・再作成は禁止。取得不能なら明示してください。\n'
            '3. この原本を変更せず使うHTML確認カードの条件を簡潔に整理し、derived提案と確定条件を区別してください。\n', encoding='utf-8')
        return {'handoff': str(destination), 'files': ['original.png', 'reference.txt', 'context.json', 'request.txt'],
                'excluded': resolution.excluded, 'model_delivery': 'not_claimed'}

    def record_client(self, evidence_path):
        evidence = json.loads(Path(evidence_path).read_text(encoding='utf-8'))
        expected = {'client', 'source', 'sha256', 'bytes', 'width', 'height', 'strict_png', 'visual_observation'}
        if set(evidence) != expected or evidence['client'] != 'chatgpt' or not isinstance(evidence['source'], str) or not evidence['source'].strip():
            raise ValueError('client evidence schema')
        if (evidence['sha256'] != ORIGINAL_SHA or evidence['bytes'] != 475 or evidence['width'] != 192
                or evidence['height'] != 64 or evidence['strict_png'] is not True
                or not isinstance(evidence['visual_observation'], str) or not evidence['visual_observation'].strip()):
            raise InsufficientContext('client original evidence incomplete')
        # Caller is the trusted local operator reviewing actual external tool/visual evidence.
        # This schema is not an independent proof of the contents of an arbitrary model report.
        self._record('same-original', 'contract', 'pass', 'operator-reviewed:' + evidence['source'])
        dump(self.root / 'evidence' / 'chatgpt-original.json', evidence)
        return self.status()

    def backup_check(self):
        adapter = self.adapter()
        with tempfile.TemporaryDirectory(prefix='nexus-recovery-') as temporary:
            base = Path(temporary)
            export_core(self.projects, self.contexts, self.artifacts, PROJECT, self.owner, base / 'bundle', approvals=adapter.approvals)
            p, c, a = restore_core(base / 'bundle', base / 'restored', self.owner)
            if p.export(PROJECT, self.owner) != self.snapshot() or c.export(PROJECT, self.owner) != self.contexts.export(PROJECT, self.owner):
                raise InsufficientContext('restored state differs')
            if a.read('original', 1, Access(self.owner, PROJECT)).sha256 != ORIGINAL_SHA:
                raise InsufficientContext('restored original differs')
        for kind in ('machine', 'contract'):
            self._record('portable', kind, 'pass', 'local:independent full bundle restore and canonical state/original comparison')
        return self.status()

    def accept(self):
        state = self.status()
        if any(item != 'card:uat' for item in state['missing']):
            raise InsufficientContext('complete machine/cross-client/recovery checks before owner UAT')
        review = self.root / 'review' / 'card.html'
        if not review.is_file() or hashlib.sha256(review.read_bytes()).hexdigest() != state['output']['sha256']:
            raise InsufficientContext('visible review file differs from bound artifact')
        if 'card:uat' in state['missing']:
            self._record('card', 'uat', 'pass', 'owner inspected: ' + str(self.root / 'review' / 'card.html'))
        return self.finish()

    def finish(self):
        self.adapter().capability.authenticate(self.owner)
        # Recheck live requirements even after DONE, recording invalidation if needed.
        self.projects.finish(PROJECT, self.owner)
        (self.root / 'review' / 'inspection.html').write_text(self.projects.inspection_html(PROJECT, self.owner), encoding='utf-8')
        return self.status()

    def export(self, destination):
        count = export_core(self.projects, self.contexts, self.artifacts, PROJECT, self.owner,
                            Path(destination), approvals=self.adapter().approvals)
        return {'backup': str(destination), 'artifact_revisions': count, 'contains_owner_capability': False}

    def status(self):
        snapshot = self.snapshot()
        if snapshot is None:
            return {'project': PROJECT, 'state': 'draft', 'next': 'start (native owner Contract and Decision confirmation)', 'root': str(self.root)}
        output = self.output()
        latest = {}
        if output:
            for e in snapshot['events']:
                p = e['payload']
                if e['kind'] == 'verification' and p['output_event'] == output['seq']:
                    latest[(p['acceptance'], p['kind'])] = p['status']
        missing = [f'{a}:{k}' for a, kinds in ACCEPTANCE.items() for k in kinds if latest.get((a,k)) != 'pass']
        return dict(project=PROJECT, state=snapshot['project']['state'], output=output['payload'] if output else None,
                    missing=missing, root=str(self.root), full_nexus_acceptance='not_claimed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'NexusCoreV2' / 'workspaces' / PROJECT)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'start', 'status', 'build', 'verify', 'handoff', 'backup-check', 'accept', 'finish'):
        sub.add_parser(command)
    correction = sub.add_parser('correct')
    correction.add_argument('body')
    correction.add_argument('--note', help='operator-written display text that implements the correction')
    correction.add_argument('--source', default='local-operator-correction')
    sub.add_parser('record-client').add_argument('evidence', type=Path)
    sub.add_parser('export').add_argument('destination', type=Path)
    args = parser.parse_args(argv)
    workflow = Workflow(args.root, getpass.getuser())
    try:
        if args.command == 'correct': result = workflow.build(correction=args.body, note=args.note, source=args.source)
        elif args.command == 'record-client': result = workflow.record_client(args.evidence)
        elif args.command == 'export': result = workflow.export(args.destination)
        else: result = getattr(workflow, args.command.replace('-', '_'))()
    except (ApprovalDeclined, InsufficientContext, AccessDenied, ValueError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
