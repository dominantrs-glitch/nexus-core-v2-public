from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import confirm_initial_owner_decision, owner_approval_probe
from nexus.artifacts import Access, ArtifactStore, LocalBinaryProvider
from nexus.context_store import ContextStore
from nexus.owner import ApprovalDeclined, ApprovalLedger, ApprovalRequest, OwnerAdapter, OwnerAuthenticationError, OwnerCapability, open_local_owner_adapter
from nexus.project import ProjectStore


class Accept:
    def __init__(self, answer=True): self.answer, self.requests = answer, []
    def confirm(self, request): self.requests.append(request); return self.answer


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.capability = OwnerCapability.bootstrap(root / "owner.capability", "owner")
        self.approvals = ApprovalLedger(root / "approvals.sqlite3")
        self.contexts = ContextStore(root / "contexts.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts.sqlite3", LocalBinaryProvider(root / "blobs"))
        self.projects = ProjectStore(root / "projects.sqlite3", context_store=self.contexts, artifact_store=self.artifacts)
        self.surface = Accept()
        self.adapter = OwnerAdapter(self.capability, self.approvals, self.projects, self.contexts, self.surface)
        self.source = self.contexts.register_source("source", "p", "owner", "fixture")

    def test_dpapi_capability_is_current_owner_only_and_tamper_fails_closed(self):
        reopened = OwnerCapability.open(self.capability.path, "owner")
        self.assertEqual(reopened.fingerprint, self.capability.fingerprint)
        with self.assertRaises(OwnerAuthenticationError): OwnerCapability.open(self.capability.path, "other")
        self.capability.path.write_text("{}", encoding="utf-8")
        with self.assertRaises(OwnerAuthenticationError): OwnerCapability.open(self.capability.path, "owner")

    def test_semantic_records_require_visible_approval_and_durable_receipts(self):
        self.adapter.confirm_decision("decision", "p", "owner", source=self.source, body="do it", projects=frozenset({"p"}), operations=frozenset({"finish"}), readers=frozenset({"owner"}))
        context = self.contexts.export("p", "owner")["contexts"][0]
        self.assertEqual(context["approval_receipt"], "local-approval-1")
        self.adapter.confirm_contract("p", "owner", goal="fixture", acceptance={"layout": ["machine", "contract", "uat"]}, constraints=[], source="owner")
        self.artifacts.register("out", "p", b"out", "text/plain", ("owner",))
        manifest = self.artifacts.describe("out", 1, Access("owner", "p"))
        self.adapter.bind_output("p", "owner", expected_revision=1, artifact_id="out", artifact_revision=1, sha256=manifest["sha256"])
        output = self.projects.export("p", "owner")["events"][-1]["seq"]
        self.adapter.record_verification("p", "owner", expected_revision=1, output_event=output, acceptance="layout", kind="machine", status="pass", source="machine")
        self.adapter.record_verification("p", "owner", expected_revision=1, output_event=output, acceptance="layout", kind="contract", status="pass", source="machine")
        self.adapter.record_verification("p", "owner", expected_revision=1, output_event=output, acceptance="layout", kind="uat", status="pass", source="user")
        events = self.projects.export("p", "owner")["events"]
        self.assertEqual(events[-1]["payload"]["approval_receipt"], "local-approval-3")
        self.assertEqual([request.kind for request in self.surface.requests], ["confirmed_decision", "confirmed_contract", "user_uat"])
        self.assertIn('fixture', self.surface.requests[1].summary)
        self.assertIn('layout', self.surface.requests[1].summary)
        self.assertIn(manifest['sha256'], self.surface.requests[2].summary)
        self.assertIn('Result: pass', self.surface.requests[2].summary)
        self.assertEqual([row["kind"] for row in self.approvals.export("owner")], ["confirmed_decision", "confirmed_contract", "user_uat"])

    def test_first_decision_shows_body_scope_revision_and_cancel_leaves_no_source(self):
        body = "A+B is the initial local owner boundary"
        self.adapter.confirm_decision_from_body("a-plus-b", "p", "owner", source_id="a-plus-b-source",
            source_body=body, body=body, projects=frozenset({"p"}), operations=frozenset({"owner-governance"}),
            readers=frozenset({"owner"}))
        request = self.surface.requests[0]
        self.assertEqual((request.kind, request.project, request.target_revision), ("confirmed_decision", "p", 1))
        self.assertIn(body, request.summary)
        self.assertIn("Operations: owner-governance", request.summary)
        recorded = self.contexts.export("p", "owner")["contexts"][0]
        self.assertEqual(recorded["approval_receipt"], "local-approval-1")

        cancelled_root = Path(self.tmp.name) / "cancelled"
        cancelled_root.mkdir()
        cancelled_contexts = ContextStore(cancelled_root / "contexts.sqlite3")
        cancelled_capability = OwnerCapability.bootstrap(cancelled_root / "owner.capability", "owner")
        cancelled_approvals = ApprovalLedger(cancelled_root / "approvals.sqlite3")
        cancelled = OwnerAdapter(cancelled_capability, cancelled_approvals, None, cancelled_contexts, Accept(False))
        with self.assertRaises(ApprovalDeclined):
            cancelled.confirm_decision_from_body("a-plus-b", "p", "owner", source_id="a-plus-b-source",
                source_body=body, body=body, projects=frozenset({"p"}), operations=frozenset({"owner-governance"}),
                readers=frozenset({"owner"}))
        self.assertEqual(cancelled_contexts.export("p", "owner"), {"schema": "nexus.context.v1", "sources": [], "contexts": [], "policies": []})
        self.assertEqual(cancelled_approvals.export("owner"), [])

    def test_decline_or_nonowner_cannot_create_semantic_record(self):
        declined = OwnerAdapter(self.capability, self.approvals, self.projects, self.contexts, Accept(False))
        with self.assertRaises(ApprovalDeclined):
            declined.confirm_contract("p", "owner", goal="fixture", acceptance={"layout": ["uat"]}, constraints=[], source="owner")
        self.assertEqual(self.approvals.export("owner"), [])
        with self.assertRaises(OwnerAuthenticationError):
            self.adapter.confirm_contract("p", "relay", goal="fixture", acceptance={"layout": ["uat"]}, constraints=[], source="relay")

    def test_factory_creates_only_local_owner_files_when_opened(self):
        root = Path(self.tmp.name) / "owner-data"
        self.assertFalse(root.exists())
        adapter = open_local_owner_adapter("owner", self.projects, self.contexts, data_root=root, surface=self.surface)
        self.assertEqual(adapter.capability.path, root / "owner.capability.json")
        self.assertTrue((root / "owner.capability.json").is_file())
        self.assertTrue((root / "approvals.sqlite3").is_file())

    def test_ux_probe_has_no_project_side_effect_and_records_only_after_ok(self):
        root = Path(self.tmp.name) / "probe"
        with patch("scripts.owner_approval_probe.getpass.getuser", return_value="owner"), \
             patch("scripts.owner_approval_probe.local_owner_data_root", return_value=root), \
             patch("scripts.owner_approval_probe.WindowsConfirmationSurface", return_value=Accept()), \
             patch("builtins.print") as output:
            self.assertEqual(owner_approval_probe.main([]), 0)
        self.assertIn("local owner UX only", output.call_args.args[0])
        self.assertEqual(ApprovalLedger(root / "approvals.sqlite3").export("owner")[0]["kind"], "owner_approval_ux_probe")
        with self.projects._db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM projects").fetchone())

    def test_ux_probe_direct_script_startup_finds_checked_in_package(self):
        script = Path(__file__).parents[1] / "scripts" / "owner_approval_probe.py"
        completed = subprocess.run([sys.executable, str(script), "--check-import"], cwd=self.tmp.name,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ready: Nexus owner UX probe can start from this script path")

    def test_initial_decision_script_is_idempotent_and_starts_directly(self):
        root = Path(self.tmp.name) / "initial-decision"
        with patch("builtins.print") as output:
            self.assertEqual(confirm_initial_owner_decision.run("owner", root, Accept()), 0)
        self.assertIn("confirmed: local-approval-1", output.call_args.args[0])
        with patch("builtins.print") as output:
            self.assertEqual(confirm_initial_owner_decision.run("owner", root, Accept()), 0)
        self.assertIn("already-confirmed: local-approval-1", output.call_args.args[0])
        script = Path(__file__).parents[1] / "scripts" / "confirm_initial_owner_decision.py"
        completed = subprocess.run([sys.executable, str(script), "--check"], cwd=self.tmp.name,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Project, body, scope, and revision 1", completed.stdout)
