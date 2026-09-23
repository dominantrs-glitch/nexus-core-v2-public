"""Record an example A+B decision through a native local dialog.

No external client, OAuth flow, Project Contract, UAT, payment, or permission
is touched.  Cancel creates no Source, confirmed Decision, or approval receipt.
"""
import argparse
import getpass
import os
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from nexus.context_store import ContextStore
from nexus.owner import ApprovalDeclined, WindowsConfirmationSurface, open_local_owner_adapter


PROJECT = "nexus-core-v2"
DECISION_ID = "initial-owner-confirmation-a-plus-b"
SOURCE_ID = "user-decision-initial-owner-confirmation-a-plus-b"
OPERATIONS = frozenset({"owner-governance"})
DECISION_BODY = """Synthetic sample: require explicit native confirmation for confirmed decisions, contracts and owner acceptance. This sample grants no external access or deployment permission."""


def _existing(snapshot):
    matches = [entry for entry in snapshot["contexts"] if entry["id"] == DECISION_ID]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("initial owner decision history is ambiguous")
    entry = matches[0]
    expected = {"id", "revision", "project", "owner", "kind", "authority", "status", "source_id", "source_revision", "body", "approval_receipt", "projects", "operations", "readers"}
    if (set(entry) != expected or entry["revision"] != 1 or entry["project"] != PROJECT
            or entry["kind"] != "decision" or entry["authority"] != "confirmed"
            or entry["status"] != "current" or entry["source_id"] != SOURCE_ID
            or entry["source_revision"] != 1 or entry["body"] != DECISION_BODY
            or entry["projects"] != [PROJECT] or entry["operations"] != sorted(OPERATIONS)
            or entry["readers"] != [entry["owner"]] or not entry["approval_receipt"]):
        raise ValueError("initial owner decision already exists with a different scope")
    return entry


def run(owner, root, surface=None):
    root.mkdir(parents=True, exist_ok=True)
    contexts = ContextStore(root / "decision-context.sqlite3")
    existing = _existing(contexts.export(PROJECT, owner))
    if existing is not None:
        print(f"already-confirmed: {existing['approval_receipt']}; confirmed Decision revision 1 remains unchanged")
        return 0
    adapter = open_local_owner_adapter(owner, None, contexts, data_root=root,
                                       surface=surface or WindowsConfirmationSurface())
    try:
        revision = adapter.confirm_decision_from_body(
            DECISION_ID, PROJECT, owner, source_id=SOURCE_ID, source_body=DECISION_BODY,
            body=DECISION_BODY, projects=frozenset({PROJECT}), operations=OPERATIONS,
            readers=frozenset({owner}), expected_revision=0, source_expected_revision=0,
        )
    except ApprovalDeclined:
        print("declined: no Source, confirmed Decision, or approval receipt was created")
        return 3
    recorded = _existing(contexts.export(PROJECT, owner))
    if recorded is None or recorded["revision"] != revision:
        raise RuntimeError("confirmed Decision was not durably recorded")
    print(f"confirmed: {recorded['approval_receipt']}; Project={PROJECT}; Decision={DECISION_ID}; revision={revision}; scope=owner-governance")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="show readiness without local writes or a dialog")
    args = parser.parse_args(argv)
    if args.check:
        print("ready: initial A+B Decision confirmation will display Project, body, scope, and revision 1")
        return 0
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA is required for the Windows-local owner confirmation")
    return run(getpass.getuser(), Path(base) / "NexusCoreV2" / "owner")


if __name__ == "__main__":
    raise SystemExit(main())
