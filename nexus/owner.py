"""Windows-local owner capability and explicit semantic approval adapter.

The DPAPI capsule is bound to the current Windows user.  It keeps relay/model
clients out of the local owner path, but does not claim to defend against a
malicious process already running as that same Windows user.  Semantic actions
therefore additionally require a visible human confirmation and durable local
approval receipt.
"""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from typing import Protocol

from nexus.artifacts import AccessDenied


class OwnerAuthenticationError(AccessDenied):
    """The process has no usable current-Windows-user owner capability."""


class ApprovalDeclined(AccessDenied):
    """The person did not explicitly approve this semantic action."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class DpapiProtector:
    """Current-user DPAPI only; there is intentionally no cross-user fallback."""
    UI_FORBIDDEN = 0x1

    @staticmethod
    def _blob(data):
        buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    def _crypt(self, name, data: bytes, entropy: bytes) -> bytes:
        if os.name != "nt":
            raise OwnerAuthenticationError("Windows DPAPI unavailable")
        source, source_buffer = self._blob(data)
        entropy_blob, entropy_buffer = self._blob(entropy)
        target = _Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if name == "protect":
            ok = crypt32.CryptProtectData(ctypes.byref(source), "Nexus Core V2 owner capability", ctypes.byref(entropy_blob), None, None, self.UI_FORBIDDEN, ctypes.byref(target))
        else:
            description = wintypes.LPWSTR()
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy_blob), None, None, self.UI_FORBIDDEN, ctypes.byref(target))
            if description:
                kernel32.LocalFree(description)
        if not ok:
            raise OwnerAuthenticationError("Windows owner capability unavailable")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            kernel32.LocalFree(target.pbData)

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt("protect", data, entropy)

    def unprotect(self, data: bytes, entropy: bytes) -> bytes:
        return self._crypt("unprotect", data, entropy)


class ConfirmationSurface(Protocol):
    def confirm(self, request: "ApprovalRequest") -> bool: ...


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str
    project: str
    target_revision: int
    summary: str


class WindowsConfirmationSurface:
    """A one-click, interactive Windows confirmation; decline on UI failure."""
    OK_CANCEL = 0x0001
    ICON_INFORMATION = 0x0040
    IDOK = 1

    def confirm(self, request: ApprovalRequest) -> bool:
        if os.name != "nt":
            return False
        title = "Nexus Core V2 — 本人確認"
        text = (f"承認種別: {request.kind}\nProject: {request.project}\n"
                f"対象revision: {request.target_revision}\n\n{request.summary}\n\nこの操作を本人確認済みとして記録しますか？")
        try:
            result = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW(None, text, title, self.OK_CANCEL | self.ICON_INFORMATION)
        except OSError:
            return False
        return result == self.IDOK


class OwnerCapability:
    FORMAT = "nexus-owner-capability-v1"

    def __init__(self, path: Path, owner: str, capability_id: str, secret: bytes):
        self.path, self.owner, self.capability_id, self._secret = path, owner, capability_id, secret

    @classmethod
    def bootstrap(cls, path: Path, owner: str, protector=None) -> "OwnerCapability":
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner required")
        protector = protector or DpapiProtector()
        if path.exists():
            return cls.open(path, owner, protector)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"format": cls.FORMAT, "owner": owner, "capability_id": secrets.token_hex(16),
                   "secret": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        capsule = protector.protect(raw, cls._entropy(owner))
        public = {"format": cls.FORMAT, "owner": owner, "capsule": base64.b64encode(capsule).decode("ascii")}
        fd, temporary = tempfile.mkstemp(prefix=".owner-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                json.dump(public, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return cls.open(path, owner, protector)

    @classmethod
    def open(cls, path: Path, owner: str, protector=None) -> "OwnerCapability":
        protector = protector or DpapiProtector()
        try:
            public = json.loads(path.read_text(encoding="utf-8"))
            if set(public) != {"format", "owner", "capsule"} or public["format"] != cls.FORMAT or public["owner"] != owner:
                raise OwnerAuthenticationError("owner capability unavailable")
            raw = protector.unprotect(base64.b64decode(public["capsule"], validate=True), cls._entropy(owner))
            payload = json.loads(raw.decode("utf-8"))
            if set(payload) != {"format", "owner", "capability_id", "secret"} or payload["format"] != cls.FORMAT or payload["owner"] != owner:
                raise OwnerAuthenticationError("owner capability unavailable")
            secret = base64.b64decode(payload["secret"], validate=True)
            if len(secret) != 32 or len(payload["capability_id"]) != 32:
                raise OwnerAuthenticationError("owner capability unavailable")
            return cls(path, owner, payload["capability_id"], secret)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError, base64.binascii.Error, OwnerAuthenticationError) as error:
            if isinstance(error, OwnerAuthenticationError):
                raise
            raise OwnerAuthenticationError("owner capability unavailable") from error

    @staticmethod
    def _entropy(owner: str) -> bytes:
        return ("Nexus Core V2 / " + owner).encode("utf-8")

    def authenticate(self, actor: str) -> None:
        if actor != self.owner or len(self._secret) != 32:
            raise OwnerAuthenticationError("owner capability unavailable")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._secret).hexdigest()


class ApprovalLedger:
    """Owner-local receipt log; no source body or private Artifact bytes are copied."""
    FORMAT = "nexus-approval-ledger-v1"
    FIELDS = ("id", "owner", "capability_id", "kind", "project", "target_revision", "summary_digest", "approved_at")
    def __init__(self, database: Path):
        self.database = database
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approvals(
                id INTEGER PRIMARY KEY, owner TEXT NOT NULL, capability_id TEXT NOT NULL,
                kind TEXT NOT NULL, project TEXT NOT NULL, target_revision INTEGER NOT NULL,
                summary_digest TEXT NOT NULL, approved_at TEXT NOT NULL)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, owner: str, capability: OwnerCapability, request: ApprovalRequest) -> str:
        capability.authenticate(owner)
        summary = json.dumps(asdict(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        with self._db() as db:
            sequence = db.execute("INSERT INTO approvals(owner,capability_id,kind,project,target_revision,summary_digest,approved_at) VALUES(?,?,?,?,?,?,?)",
                (owner, capability.capability_id, request.kind, request.project, request.target_revision, digest,
                 datetime.now(timezone.utc).isoformat())).lastrowid
        return f"local-approval-{sequence}"

    def export(self, owner: str):
        with self._db() as db:
            return [dict(zip(self.FIELDS, row))
                    for row in db.execute("SELECT * FROM approvals WHERE owner=? ORDER BY id", (owner,))]

    def export_project(self, owner: str, project: str):
        """Export only immutable receipt metadata for one Project.

        The capability secret and its DPAPI capsule are deliberately excluded:
        restored receipts remain an audit trail, not a portable login method.
        """
        if not isinstance(project, str) or not project:
            raise ValueError("project required")
        with self._db() as db:
            approvals = [dict(zip(self.FIELDS, row)) for row in db.execute(
                "SELECT * FROM approvals WHERE owner=? AND project=? ORDER BY id", (owner, project))]
        return {"format": self.FORMAT, "owner": owner, "project": project, "approvals": approvals}

    def restore_project(self, snapshot, owner: str, project: str) -> int:
        """Restore a verified project-scoped receipt audit into an empty ledger."""
        if not isinstance(snapshot, dict) or set(snapshot) != {"format", "owner", "project", "approvals"}:
            raise ValueError("unsupported approval export")
        if snapshot["format"] != self.FORMAT or snapshot["owner"] != owner or snapshot["project"] != project or not isinstance(snapshot["approvals"], list):
            raise ValueError("unsupported approval export")
        entries = snapshot["approvals"]
        previous = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != set(self.FIELDS):
                raise ValueError("invalid approval receipt")
            receipt_id = entry["id"]
            if (not isinstance(receipt_id, int) or isinstance(receipt_id, bool) or receipt_id <= previous
                    or entry["owner"] != owner or entry["project"] != project
                    or not isinstance(entry["capability_id"], str) or len(entry["capability_id"]) != 32
                    or not isinstance(entry["kind"], str) or not entry["kind"]
                    or not isinstance(entry["target_revision"], int) or isinstance(entry["target_revision"], bool) or entry["target_revision"] < 0
                    or not isinstance(entry["summary_digest"], str) or len(entry["summary_digest"]) != 64
                    or not isinstance(entry["approved_at"], str)):
                raise ValueError("invalid approval receipt")
            try:
                int(entry["capability_id"], 16)
                int(entry["summary_digest"], 16)
                datetime.fromisoformat(entry["approved_at"])
            except ValueError as error:
                raise ValueError("invalid approval receipt") from error
            previous = receipt_id
        with self._db() as db:
            if db.execute("SELECT 1 FROM approvals LIMIT 1").fetchone():
                raise ValueError("approval restore requires an empty ledger")
            db.executemany(
                "INSERT INTO approvals(id,owner,capability_id,kind,project,target_revision,summary_digest,approved_at) VALUES(?,?,?,?,?,?,?,?)",
                [tuple(entry[field] for field in self.FIELDS) for entry in entries],
            )
        return len(entries)


class OwnerAdapter:
    """A = local capability for routine work; B = explicit approval for semantics."""
    def __init__(self, capability: OwnerCapability, approvals: ApprovalLedger, projects, contexts, surface=None):
        self.capability, self.approvals = capability, approvals
        self.projects, self.contexts = projects, contexts
        self.surface = surface or WindowsConfirmationSurface()

    def _routine(self, actor):
        self.capability.authenticate(actor)

    def _semantic(self, actor, kind, project, target_revision, summary):
        self._routine(actor)
        request = ApprovalRequest(kind, project, target_revision, summary)
        if not self.surface.confirm(request):
            raise ApprovalDeclined("owner approval declined")
        return self.approvals.record(actor, self.capability, request)

    @staticmethod
    def _decision_summary(identity, body, projects, operations, readers):
        return (f"Decision ID: {identity}\n\n本文:\n{body}\n\nScope:\n"
                f"Projects: {', '.join(sorted(projects))}\n"
                f"Operations: {', '.join(sorted(operations))}\n"
                f"Readers: {', '.join(sorted(readers))}")

    def confirm_contract(self, project, actor, *, goal, acceptance, constraints, source, expected_revision=0):
        self._routine(actor)
        self.projects._validate_contract(goal, acceptance, constraints, source)
        summary = (f"Goal:\n{goal}\n\nAcceptance（必須検証）:\n" +
                   json.dumps(acceptance, ensure_ascii=False, indent=2) +
                   "\n\nConstraints:\n" + "\n".join(constraints) + f"\n\nSource: {source}")
        receipt = self._semantic(actor, "confirmed_contract", project, expected_revision + 1, summary)
        return self.projects.confirm_contract(project, actor, goal=goal, acceptance=acceptance, constraints=constraints,
            source=source, expected_revision=expected_revision, approval_receipt=receipt)

    def confirm_decision(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current"):
        receipt = self._semantic(actor, "confirmed_decision", project, expected_revision + 1,
                                 self._decision_summary(identity, body, projects, operations, readers))
        return self.contexts.confirm_decision(identity, project, actor, source=source, body=body, projects=projects,
            operations=operations, readers=readers, expected_revision=expected_revision, status=status, approval_receipt=receipt)

    def confirm_decision_from_body(self, identity, project, actor, *, source_id, source_body,
                                   body, projects, operations, readers, expected_revision=0,
                                   source_expected_revision=0, status="current"):
        """Confirm a new Decision without persisting its Source on Cancel.

        This is for a local, first-time human-facing workflow.  The visible
        confirmation runs before the Source is appended; a decline therefore
        creates neither a receipt nor a Source/Decision record.
        """
        self._routine(actor)
        request = ApprovalRequest("confirmed_decision", project, expected_revision + 1,
                                  self._decision_summary(identity, body, projects, operations, readers))
        if not self.surface.confirm(request):
            raise ApprovalDeclined("owner approval declined")
        source = self.contexts.register_source(source_id, project, actor, source_body,
                                                expected_revision=source_expected_revision)
        receipt = self.approvals.record(actor, self.capability, request)
        return self.contexts.confirm_decision(identity, project, actor, source=source, body=body,
            projects=projects, operations=operations, readers=readers,
            expected_revision=expected_revision, status=status, approval_receipt=receipt)

    def bind_output(self, project, actor, **kwargs):
        self._routine(actor)
        return self.projects.bind_output(project, actor, **kwargs)

    def record_verification(self, project, actor, *, kind, **kwargs):
        self._routine(actor)
        if kind == "uat":
            snapshot = self.projects.export(project, actor)
            output = next((e for e in reversed(snapshot['events']) if e['kind'] == 'output'), None)
            if (snapshot['project']['revision'] != kwargs['expected_revision'] or output is None
                    or output['seq'] != kwargs['output_event']):
                raise ValueError('stale verification target')
            summary = (f"Acceptance: {kwargs['acceptance']}\nResult: {kwargs['status']}\n"
                       f"Artifact: {output['payload']['artifact_id']} / revision {output['payload']['revision']}\n"
                       f"SHA-256: {output['payload']['sha256']}\nEvidence: {kwargs['source']}\n\n"
                       "表示された成果物を実際に確認した結果だけを承認してください。")
            receipt = self._semantic(actor, "user_uat", project, kwargs["expected_revision"], summary)
            return self.projects.record_verification(project, actor, kind=kind, approval_receipt=receipt, **kwargs)
        return self.projects.record_verification(project, actor, kind=kind, **kwargs)


def local_owner_data_root() -> Path:
    """Per-user local application data; no cloud sync, service, or auto-start."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise OwnerAuthenticationError("local application data unavailable")
    return Path(base) / "NexusCoreV2" / "owner"


def open_local_owner_adapter(owner: str, projects, contexts, *, data_root: Path | None = None, surface=None) -> OwnerAdapter:
    """Open A+B local wiring. Creating this adapter creates only local owner files."""
    root = data_root or local_owner_data_root()
    capability = OwnerCapability.bootstrap(root / "owner.capability.json", owner)
    return OwnerAdapter(capability, ApprovalLedger(root / "approvals.sqlite3"), projects, contexts, surface)
