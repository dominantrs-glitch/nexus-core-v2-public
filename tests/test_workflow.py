import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from nexus.artifacts import Access, InsufficientContext
from nexus.core_backup import restore_core
from nexus.owner import ApprovalDeclined
from nexus.workflow import Workflow, PROJECT, ORIGINAL_SHA


class Surface:
    def __init__(self):
        self.allow = True
        self.requests = []
    def confirm(self, request):
        self.requests.append(request)
        return self.allow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.surface = Surface()
        self.w = Workflow(self.root / 'workspace', 'fixture-owner', owner_root=self.root / 'owner', surface=self.surface)

    def client_fixture(self):
        evidence = dict(client='chatgpt', source='unit-test simulated client, not live E2E', sha256=ORIGINAL_SHA,
                        bytes=475, width=192, height=64, strict_png=True, visual_observation='synthetic test observation')
        path = self.root / 'client.json'
        path.write_text(json.dumps(evidence), encoding='utf-8')
        return path

    def test_complete_local_loop_resume_and_restore_without_fake_live_claim(self):
        self.w.start()
        self.w.start()
        self.assertEqual(len(self.surface.requests), 2)
        self.w.build()
        initial = self.w.snapshot()
        self.w.build()
        self.assertEqual(self.w.snapshot(), initial)
        self.w.build(correction='修正内容をカードに明示してください。', note='確認した範囲と未確認を区別します。', source='synthetic reviewer fixture')
        revised = self.w.snapshot()
        self.w.build(correction='修正内容をカードに明示してください。')
        self.assertEqual(self.w.snapshot(), revised)
        self.assertIn('確認した範囲と未確認を区別します。', (self.w.root / 'review' / 'card.html').read_text(encoding='utf-8'))
        self.w.record_client(self.client_fixture())
        self.w.backup_check()
        self.assertEqual(self.w.status()['missing'], ['card:uat'])
        self.w.accept()
        self.assertEqual(self.w.status()['state'], 'done')
        approvals = len(self.surface.requests)
        self.w.accept()
        self.assertEqual(len(self.surface.requests), approvals)
        self.w.export(self.root / 'bundle')
        p, c, a = restore_core(self.root / 'bundle', self.root / 'restored', 'fixture-owner')
        self.assertEqual(p.export(PROJECT, 'fixture-owner'), self.w.snapshot())
        self.assertEqual(a.read('original', 1, Access('fixture-owner', PROJECT)).sha256, ORIGINAL_SHA)
        self.assertFalse((self.root / 'bundle' / 'owner.capability.json').exists())

    def test_cancel_start_leaves_no_confirmed_contract_or_decision(self):
        self.surface.allow = False
        with self.assertRaises(ApprovalDeclined): self.w.start()
        self.assertEqual(self.w.status()['state'], 'draft')
        self.assertEqual(self.w.contexts.export(PROJECT, 'fixture-owner')['contexts'], [])

    def test_handoff_selects_authorized_context_and_exact_original(self):
        self.w.start()
        result = self.w.handoff()
        directory = Path(result['handoff'])
        raw = (directory / 'context.json').read_text(encoding='utf-8')
        self.assertNotIn('DO_NOT_DELIVER', raw)
        self.assertNotIn('fixture-owner', raw)
        self.assertNotIn('approval', raw)
        self.assertEqual((directory / 'original.png').read_bytes(), self.w.artifacts.read('original',1,Access('fixture-owner', PROJECT)).data)
        self.assertGreaterEqual(result['excluded'], 3)
        contexts = json.loads(raw)['context']
        self.assertTrue(next(c for c in contexts if c['id']=='slice-decision')['binding'])
        self.assertFalse(next(c for c in contexts if c['id']=='proposal')['binding'])

    def test_no_done_or_uat_when_client_evidence_is_missing(self):
        self.w.start()
        self.w.build()
        self.w.build(correction='修正を記録する。')
        self.w.backup_check()
        prompts = len(self.surface.requests)
        with self.assertRaises(InsufficientContext): self.w.accept()
        with self.assertRaises(InsufficientContext): self.w.finish()
        self.assertEqual(prompts, len(self.surface.requests))
        self.assertNotEqual(self.w.status()['state'], 'done')

    def test_restore_project_with_event_gaps_from_other_project(self):
        self.w.start()
        self.w.projects.confirm_contract('other', 'fixture-owner', goal='other', acceptance={'test':['machine']}, constraints=[], source='fixture')
        self.w.build()
        self.w.export(self.root / 'bundle')
        p, _, _ = restore_core(self.root / 'bundle', self.root / 'restored', 'fixture-owner')
        self.assertEqual(p.export(PROJECT,'fixture-owner'), self.w.snapshot())

    def test_client_mismatch_never_becomes_pass(self):
        self.w.start()
        self.w.build()
        path = self.client_fixture()
        evidence = json.loads(path.read_text())
        evidence['sha256'] = '0' * 64
        path.write_text(json.dumps(evidence))
        with self.assertRaises(InsufficientContext): self.w.record_client(path)
        self.assertIn('same-original:contract', self.w.status()['missing'])

    def test_missing_required_reference_stops_delivery_and_records_generic_trace(self):
        self.w.start()
        manifest = self.w.artifacts.describe('reference',1,Access('fixture-owner',PROJECT))
        (self.w.root / 'blobs' / manifest['sha256']).unlink()
        with self.assertRaises(InsufficientContext): self.w.handoff()
        self.assertFalse((self.w.root / 'handoff').exists())
        event = self.w.snapshot()['events'][-1]
        self.assertEqual(event['kind'], 'context_gate')
        self.assertEqual(event['payload'], {'operation':'handoff','result':'insufficient_context'})

    def test_uat_cannot_approve_a_modified_review_file(self):
        self.w.start()
        self.w.build()
        self.w.build(correction='確認範囲の説明を表示する。')
        self.w.record_client(self.client_fixture())
        self.w.backup_check()
        (self.w.root / 'review' / 'card.html').write_text('different visible artifact', encoding='utf-8')
        prompts = len(self.surface.requests)
        with self.assertRaises(InsufficientContext): self.w.accept()
        self.assertEqual(len(self.surface.requests), prompts)

    def test_menu_starts_in_legacy_windows_console_encoding(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'nexus_operator.py'
        run = subprocess.run([sys.executable, str(script)], input='0\n', text=True,
            encoding='cp932', capture_output=True, cwd=self.root,
            env={**os.environ, 'PYTHONIOENCODING': 'cp932'})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('合成案件のローカル操作', run.stdout)


if __name__ == '__main__':
    unittest.main()
