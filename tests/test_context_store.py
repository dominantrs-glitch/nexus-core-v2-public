from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from nexus.artifacts import Access, ArtifactStore, InsufficientContext, LocalBinaryProvider
from nexus.context_store import ContextStore
from nexus.project import ProjectStore


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.context = ContextStore(root / "context.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts.sqlite3", LocalBinaryProvider(root / "blobs"))
        self.project = ProjectStore(root / "projects.sqlite3", context_store=self.context, artifact_store=self.artifacts)
        self.source = self.context.register_source("source", "p", "owner", "trusted fixture")

    def decision(self, **changes):
        args = dict(source=self.source, body="confirmed instruction", projects=frozenset({"p"}),
                    operations=frozenset({"finish"}), readers=frozenset({"owner"}))
        args.update(changes)
        return self.context.confirm_decision("decision", "p", "owner", **args)

    def test_immutable_source_and_context_history_are_exported_but_not_resolved(self):
        self.decision()
        newer = self.context.register_source("source", "p", "owner", "corrected source", expected_revision=1)
        self.decision(source=newer, body="superseded instruction", expected_revision=1, status="superseded")
        with self.assertRaises(InsufficientContext):
            self.context.resolve("p", "owner", "finish", required=frozenset({"decision"}), required_authority={"decision": "confirmed"})
        exported = self.context.export("p", "owner")
        self.assertEqual([item["revision"] for item in exported["sources"]], [1, 2])
        self.assertEqual([item["revision"] for item in exported["contexts"]], [1, 2])
        self.assertEqual(exported["contexts"][1]["source_revision"], 2)

    def test_approved_decision_receipt_survives_context_export_restore(self):
        self.decision(approval_receipt="local-approval-7")
        exported = self.context.export("p", "owner")
        restored = ContextStore(Path(self.tmp.name) / "restored-context.sqlite3")
        restored.restore_export(exported, "owner")
        self.assertEqual(restored.export("p", "owner"), exported)

    def test_scoped_non_delivery_and_authority_cannot_be_forged(self):
        self.context.save_personal_context("private", "p", "owner", source=self.source, body="private",
            projects=frozenset({"p"}), operations=frozenset({"finish"}), readers=frozenset({"someone-else"}))
        self.decision()
        resolution = self.context.resolve("p", "owner", "finish", required=frozenset({"decision"}), required_authority={"decision": "confirmed"})
        self.assertEqual([item.record.id for item in resolution.items], ["decision"])
        with self.assertRaises(AttributeError):
            self.context.record_context
        with self.assertRaises(ValueError):
            self.context.derive("decision", "p", "owner", source=self.source, body="fake", projects=frozenset({"p"}),
                operations=frozenset({"finish"}), readers=frozenset({"owner"}), expected_revision=0)

    def test_persisted_policy_integrates_live_context_and_artifact_done_gate(self):
        self.decision()
        self.project.confirm_contract("p", "owner", goal="synthetic", acceptance={"layout": ["machine", "contract", "uat"]}, constraints=[], source="owner")
        self.context.set_operation_policy("p", "owner", contract_revision=1, operation="finish", principal="owner",
            required_context=frozenset({"decision"}), required_authority={"decision": "confirmed"})
        self.artifacts.register("out", "p", b"version 1", "text/plain", ("owner",))
        manifest = self.artifacts.describe("out", 1, Access("owner", "p"))
        self.project.bind_output("p", "owner", expected_revision=1, artifact_id="out", artifact_revision=1, sha256=manifest["sha256"])
        output = self.project.export("p", "owner")["events"][-1]["seq"]
        for kind in ("machine", "contract", "uat"):
            self.project.record_verification("p", "owner", expected_revision=1, output_event=output, acceptance="layout", kind=kind, status="pass", source="fixture")
        self.project.finish("p", "owner")
        self.assertEqual(self.project.export("p", "owner")["project"]["state"], "done")
        self.context.derive("decision", "p", "owner", source=self.source, body="not confirmed", projects=frozenset({"p"}),
            operations=frozenset({"finish"}), readers=frozenset({"owner"}), expected_revision=1)
        with self.assertRaises(InsufficientContext):
            self.project.finish("p", "owner")
        exported = self.project.export("p", "owner")
        self.assertEqual(exported["project"]["state"], "blocked")
        gate = exported["events"][-1]["payload"]
        self.assertEqual(gate["trace"]["category"], "insufficient_context")
        self.assertEqual(gate["trace"]["detail"], "required context unavailable or unresolved")

    def test_policy_is_revision_bound_and_owner_only(self):
        self.decision()
        self.context.set_operation_policy("p", "owner", contract_revision=1, operation="finish", principal="owner",
            required_context=frozenset({"decision"}), required_authority={"decision": "confirmed"}, required_artifacts=(("reference", 1),))
        with self.assertRaises(InsufficientContext): self.context.policy("p", "other", 1, "finish")
        with self.assertRaises(ValueError):
            self.context.set_operation_policy("p", "owner", contract_revision=1, operation="finish", principal="owner",
                required_context=frozenset({"decision"}), required_authority={"decision": "confirmed"})
        policy = self.context.policy("p", "owner", 1, "finish")
        self.assertEqual(policy.required_artifacts, (("reference", 1),))
