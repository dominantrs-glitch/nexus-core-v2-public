import json
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import Access, ArtifactStore, InsufficientContext, LocalBinaryProvider
from nexus.context_store import ContextStore
from nexus.core_backup import export_core, restore_core
from nexus.owner import ApprovalLedger, ApprovalRequest, OwnerCapability
from nexus.project import ProjectStore


class CoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.contexts = ContextStore(root / "context.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts.sqlite3", LocalBinaryProvider(root / "blobs"))
        self.projects = ProjectStore(root / "projects.sqlite3", context_store=self.contexts, artifact_store=self.artifacts)
        self.approvals = ApprovalLedger(root / "approvals.sqlite3")
        self.capability = OwnerCapability.bootstrap(root / "owner.capability", "owner")
        source = self.contexts.register_source("source", "p", "owner", "fixture source")
        self.contexts.confirm_decision("decision", "p", "owner", source=source, body="do it", projects=frozenset({"p"}), operations=frozenset({"finish"}), readers=frozenset({"owner"}))
        self.projects.confirm_contract("p", "owner", goal="fixture", acceptance={"layout": ["machine", "contract", "uat"]}, constraints=[], source="owner")
        self.contexts.set_operation_policy("p", "owner", contract_revision=1, operation="finish", principal="owner", required_context=frozenset({"decision"}), required_authority={"decision": "confirmed"})
        self.artifacts.register("out", "p", b"first", "text/plain", ("owner",))
        manifest = self.artifacts.describe("out", 1, Access("owner", "p"))
        self.projects.bind_output("p", "owner", expected_revision=1, artifact_id="out", artifact_revision=1, sha256=manifest["sha256"])
        self.output = self.projects.export("p", "owner")["events"][-1]["seq"]
        for kind in ("machine", "contract", "uat"):
            self.projects.record_verification("p", "owner", expected_revision=1, output_event=self.output, acceptance="layout", kind=kind, status="pass", source="fixture")
        self.projects.finish("p", "owner")

    def test_success_and_rework_history_restore_with_context_and_original(self):
        self.projects.correction("p", "owner", expected_revision=1, output_event=self.output, acceptance="layout", body="rework", source="owner")
        self.assertEqual(self.projects.export("p", "owner")["project"]["state"], "rework")
        root = Path(self.tmp.name)
        self.assertEqual(export_core(self.projects, self.contexts, self.artifacts, "p", "owner", root / "export"), 1)
        projects, contexts, artifacts = restore_core(root / "export", root / "restored", "owner")
        restored = projects.export("p", "owner")
        self.assertEqual(restored, self.projects.export("p", "owner"))
        self.assertEqual(contexts.export("p", "owner"), self.contexts.export("p", "owner"))
        self.assertEqual(artifacts.read("out", 1, Access("owner", "p")).data, b"first")

    def test_tampered_original_refuses_restore_before_ledger_entry_exists(self):
        root = Path(self.tmp.name)
        export_core(self.projects, self.contexts, self.artifacts, "p", "owner", root / "export")
        blob = next((root / "export" / "artifacts" / "blobs").iterdir())
        blob.write_bytes(b"tampered")
        with self.assertRaises(InsufficientContext): restore_core(root / "export", root / "bad", "owner")
        self.assertFalse((root / "bad" / "projects.sqlite3").exists())

    def test_project_scoped_approval_audit_restores_without_owner_capability(self):
        self.approvals.record("owner", self.capability, ApprovalRequest("confirmed_contract", "p", 1, "fixture"))
        self.approvals.record("owner", self.capability, ApprovalRequest("user_uat", "other-project", 1, "unrelated"))
        root = Path(self.tmp.name)
        export_core(self.projects, self.contexts, self.artifacts, "p", "owner", root / "export", approvals=self.approvals)
        self.assertEqual(json.loads((root / "export" / "manifest.json").read_text(encoding="utf-8"))["format"], "nexus-core-v2")
        self.assertFalse((root / "export" / "owner.capability").exists())
        restore_core(root / "export", root / "restored", "owner")
        self.assertEqual(ApprovalLedger(root / "restored" / "approvals.sqlite3").export("owner"), self.approvals.export_project("owner", "p")["approvals"])
