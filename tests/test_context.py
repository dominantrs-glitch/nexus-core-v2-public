from dataclasses import replace
import unittest

from nexus.artifacts import Access, InsufficientContext
from nexus.context import ContextRecord, resolve_context


class ContextTests(unittest.TestCase):
    def record(self, **changes):
        data = dict(id="decision", revision=1, kind="decision", authority="confirmed",
                    status="current", projects=frozenset({"synthetic"}),
                    operations=frozenset({"implement"}), readers=frozenset({"owner"}),
                    source_id="source-1", body="synthetic policy")
        data.update(changes)
        return ContextRecord(**data)

    def resolve(self, records, required=frozenset()):
        return resolve_context(tuple(records), Access("owner", "synthetic"), "implement", required=required)

    def test_authority_and_scope_are_not_latest_write_wins(self):
        current = self.record()
        old = replace(current, revision=99, status="superseded", body="old")
        derived = self.record(id="proposal", authority="derived")
        personal = self.record(id="personal", kind="personal")
        irrelevant = replace(personal, id="irrelevant", operations=frozenset({"other"}), body="secret")
        result = self.resolve([irrelevant, old, derived, personal, current], frozenset({"decision", "personal"}))
        self.assertEqual([i.record.id for i in result.items], ["decision", "personal", "proposal"])
        self.assertEqual([i.binding for i in result.items], [True, False, False])
        self.assertEqual(result.excluded, 2)
        self.assertNotIn("secret", repr(result))

    def test_required_missing_denied_stale_unknown_and_ambiguous_fail_closed(self):
        r = self.record()
        cases = [[], [replace(r, readers=frozenset({"other"}))],
                 [replace(r, projects=frozenset({"elsewhere"}))],
                 [replace(r, status="historical")], [replace(r, authority="unknown")],
                 [r, replace(r, revision=2)]]
        errors = []
        for records in cases:
            with self.assertRaises(InsufficientContext) as error:
                self.resolve(records, frozenset({"decision"}))
            errors.append(str(error.exception))
        self.assertEqual(len(set(errors)), 1)

    def test_optional_non_delivery_can_continue(self):
        for field, value in [("readers",frozenset({"other"})), ("projects",frozenset({"other"})),
                             ("operations",frozenset({"other"})), ("status","suppressed")]:
            r = replace(self.record(kind="personal", body="private"), **{field:value})
            result = self.resolve([r])
            self.assertEqual(result.items, ())
            self.assertEqual(result.excluded, 1)
            self.assertNotIn("private",repr(result))

    def test_candidate_keeps_provenance_and_is_never_binding(self):
        r = self.record(kind="candidate", authority="candidate")
        item = self.resolve([r]).items[0]
        self.assertFalse(item.binding)
        self.assertEqual(item.record.source_id,"source-1")
        with self.assertRaises(ValueError): replace(r, authority="confirmed")

    def test_schema_and_duplicate_revision_rejected(self):
        with self.assertRaises(ValueError): self.record(revision=True)
        with self.assertRaises(ValueError): self.record(source_id="")
        with self.assertRaises(ValueError): self.record(readers=frozenset())
        r = self.record()
        with self.assertRaises(ValueError): self.resolve([r,r])

    def test_derived_record_cannot_replace_required_confirmed_authority(self):
        with self.assertRaises(InsufficientContext):
            resolve_context((self.record(authority="derived"),), Access("owner","synthetic"),
                            "implement", required=frozenset({"decision"}), required_authority={"decision":"confirmed"})
