"""Show the one-click local owner-approval UX without changing a Project.

This creates only the local A+B owner files under LOCALAPPDATA and records an
``owner_approval_ux_probe`` receipt when the user presses OK.  It never sends
data externally and never creates a Contract, Decision, UAT, or DONE record.
"""
import argparse
import getpass
from pathlib import Path
import sys

# A directly executed script has scripts/ as sys.path[0], not the repository
# root.  Make the checked-in package importable without asking the user to set
# PYTHONPATH or change their shell profile.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from nexus.owner import ApprovalLedger, ApprovalRequest, OwnerCapability, WindowsConfirmationSurface, local_owner_data_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-import", action="store_true", help="verify direct-script startup without local writes or a dialog")
    args = parser.parse_args(argv)
    if args.check_import:
        print("ready: Nexus owner UX probe can start from this script path")
        return 0
    owner = getpass.getuser()
    root = local_owner_data_root()
    capability = OwnerCapability.bootstrap(root / "owner.capability.json", owner)
    request = ApprovalRequest("owner_approval_ux_probe", "local-ux-check", 1,
                              "実Projectを変更しない表示・Cancel確認")
    if not WindowsConfirmationSurface().confirm(request):
        print("declined: no approval receipt; no Project/Context/UAT was changed")
        return 3
    receipt = ApprovalLedger(root / "approvals.sqlite3").record(owner, capability, request)
    print(f"confirmed: {receipt}; local owner UX only; no Project/Context/UAT was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
