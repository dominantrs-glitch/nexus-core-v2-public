"""Durable, trusted-owner Context governance and operation policies.

This module is deliberately local application infrastructure.  It has no MCP
tool surface and does not accept an authority string from a model/client.  The
four owner methods below select the only allowed authority for their purpose.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

from nexus.artifacts import Access, AccessDenied, ArtifactStore, InsufficientContext
from nexus.context import ContextRecord, Resolution, resolve_context


AUTHORITIES = frozenset({"confirmed", "verified", "derived", "candidate"})
KINDS = frozenset({"decision", "personal", "reference", "candidate"})
STATUSES = frozenset({"current", "historical", "superseded", "suppressed"})


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty text required")
    return value


def _names(values, label: str) -> frozenset[str]:
    if not isinstance(values, frozenset) or not values or not all(isinstance(v, str) and v for v in values):
        raise ValueError(f"explicit {label} required")
    return values


@dataclass(frozen=True)
class SourceRef:
    id: str
    revision: int
    sha256: str


@dataclass(frozen=True)
class OperationPolicy:
    project: str
    contract_revision: int
    operation: str
    revision: int
    principal: str
    required_context: frozenset[str]
    required_authority: dict[str, str]
    required_artifacts: tuple[tuple[str, int], ...]


class ContextStore:
    """One SQLite ledger with immutable Sources, Context revisions and policies.

    A Context identity returns its newest stored revision to the resolver.  The
    old revisions remain in the export, while an explicitly historical,
    suppressed, or superseded latest revision is never silently selected.
    """
    def __init__(self, database: Path):
        self.database = database
        with self._db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS sources(
                id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                project TEXT NOT NULL, owner TEXT NOT NULL, body TEXT NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(id, revision));
              CREATE TABLE IF NOT EXISTS contexts(
                id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                project TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
                authority TEXT NOT NULL, status TEXT NOT NULL,
                source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                body TEXT NOT NULL, approval_receipt TEXT, PRIMARY KEY(id, revision));
              CREATE TABLE IF NOT EXISTS context_projects(
                id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(id, revision, value),
                FOREIGN KEY(id, revision) REFERENCES contexts(id, revision));
              CREATE TABLE IF NOT EXISTS context_operations(
                id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(id, revision, value),
                FOREIGN KEY(id, revision) REFERENCES contexts(id, revision));
              CREATE TABLE IF NOT EXISTS context_readers(
                id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(id, revision, value),
                FOREIGN KEY(id, revision) REFERENCES contexts(id, revision));
              CREATE TABLE IF NOT EXISTS policies(
                project TEXT NOT NULL, contract_revision INTEGER NOT NULL,
                operation TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                owner TEXT NOT NULL, principal TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(project, contract_revision, operation, revision));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(contexts)")}
            if "approval_receipt" not in columns:
                db.execute("ALTER TABLE contexts ADD COLUMN approval_receipt TEXT")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def register_source(self, source_id: str, project: str, actor: str, body: str, *, expected_revision=0) -> SourceRef:
        """Add an immutable source revision from the trusted local owner path."""
        for value in (source_id, project, actor, body):
            _text(value)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid revision")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._db() as db:
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM sources WHERE id=?", (source_id,)).fetchone()[0]
            prior = db.execute("SELECT project,owner FROM sources WHERE id=? ORDER BY revision LIMIT 1", (source_id,)).fetchone()
            if current != expected_revision:
                raise ValueError("revision conflict")
            if prior and (prior["project"] != project or prior["owner"] != actor):
                raise AccessDenied("source unavailable")
            revision = current + 1
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?)", (source_id, revision, project, actor, body, digest))
            return SourceRef(source_id, revision, digest)

    def _source(self, db, source: SourceRef, project: str, actor: str):
        if not isinstance(source, SourceRef):
            raise ValueError("durable source reference required")
        row = db.execute("SELECT * FROM sources WHERE id=? AND revision=?", (source.id, source.revision)).fetchone()
        if row is None or row["project"] != project or row["owner"] != actor or row["sha256"] != source.sha256:
            raise AccessDenied("source unavailable")
        return row

    def _record(self, identity: str, project: str, actor: str, *, kind: str, authority: str,
                status: str, source: SourceRef, body: str, projects: frozenset[str],
                operations: frozenset[str], readers: frozenset[str], expected_revision: int,
                approval_receipt: str | None = None) -> int:
        for value in (identity, project, actor, body):
            _text(value)
        if kind not in KINDS or authority not in AUTHORITIES or status not in STATUSES:
            raise ValueError("invalid context governance")
        if kind == "candidate" and authority != "candidate":
            raise ValueError("candidate authority required")
        if kind != "candidate" and authority == "candidate":
            raise ValueError("candidate kind required")
        _names(projects, "projects")
        _names(operations, "operations")
        _names(readers, "readers")
        # The trusted owner may record Context for a different permitted reader;
        # writer ownership and reader delivery are intentionally separate.
        if project not in projects or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("owner scope or revision invalid")
        if approval_receipt is not None:
            _text(approval_receipt)
        with self._db() as db:
            self._source(db, source, project, actor)
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM contexts WHERE id=?", (identity,)).fetchone()[0]
            prior = db.execute("SELECT project,owner FROM contexts WHERE id=? ORDER BY revision LIMIT 1", (identity,)).fetchone()
            if current != expected_revision:
                raise ValueError("revision conflict")
            if prior and (prior["project"] != project or prior["owner"] != actor):
                raise AccessDenied("context unavailable")
            revision = current + 1
            db.execute("INSERT INTO contexts VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, revision, project, actor, kind, authority, status, source.id, source.revision, body, approval_receipt))
            for table, values in (("context_projects", projects), ("context_operations", operations), ("context_readers", readers)):
                db.executemany(f"INSERT INTO {table} VALUES(?,?,?)", [(identity, revision, value) for value in sorted(values)])
            return revision

    # Purpose-specific entry points intentionally select authority server-side.
    def confirm_decision(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current", approval_receipt=None):
        return self._record(identity, project, actor, kind="decision", authority="confirmed", status=status,
                            source=source, body=body, projects=projects, operations=operations, readers=readers, expected_revision=expected_revision, approval_receipt=approval_receipt)

    def save_personal_context(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current"):
        return self._record(identity, project, actor, kind="personal", authority="confirmed", status=status,
                            source=source, body=body, projects=projects, operations=operations, readers=readers, expected_revision=expected_revision)

    def verify_reference(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current"):
        return self._record(identity, project, actor, kind="reference", authority="verified", status=status,
                            source=source, body=body, projects=projects, operations=operations, readers=readers, expected_revision=expected_revision)

    def derive(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current"):
        return self._record(identity, project, actor, kind="reference", authority="derived", status=status,
                            source=source, body=body, projects=projects, operations=operations, readers=readers, expected_revision=expected_revision)

    def candidate(self, identity, project, actor, *, source, body, projects, operations, readers, expected_revision=0, status="current"):
        return self._record(identity, project, actor, kind="candidate", authority="candidate", status=status,
                            source=source, body=body, projects=projects, operations=operations, readers=readers, expected_revision=expected_revision)

    def _records(self, db, project: str, actor: str) -> tuple[ContextRecord, ...]:
        rows = db.execute("""SELECT c.* FROM contexts c JOIN (
              SELECT id,MAX(revision) revision FROM contexts GROUP BY id) latest
              ON c.id=latest.id AND c.revision=latest.revision
              WHERE c.owner=? AND EXISTS (
                SELECT 1 FROM context_projects scope WHERE scope.id=c.id
                AND scope.revision=c.revision AND scope.value=?)
              ORDER BY c.id,c.revision""", (actor, project)).fetchall()
        result = []
        for row in rows:
            values = []
            for table in ("context_projects", "context_operations", "context_readers"):
                values.append(frozenset(r[0] for r in db.execute(
                    f"SELECT value FROM {table} WHERE id=? AND revision=? ORDER BY value", (row["id"], row["revision"]))))
            result.append(ContextRecord(row["id"], row["revision"], row["kind"], row["authority"], row["status"],
                *values, row["source_id"], row["body"], row["source_revision"], row["approval_receipt"]))
        return tuple(result)

    def resolve(self, project: str, actor: str, operation: str, *, required: frozenset[str], required_authority=None) -> Resolution:
        with self._db() as db:
            records = self._records(db, project, actor)
        return resolve_context(records, Access(actor, project), operation, required=required,
                               required_authority=required_authority)

    def set_operation_policy(self, project, actor, *, contract_revision, operation, principal,
                             required_context, required_authority, required_artifacts=(), expected_revision=0):
        """Versioned local policy; this is not a client/model supplied operation."""
        for value in (project, actor, operation, principal):
            _text(value)
        if principal != actor:
            # This local-only increment has no authenticated delegation adapter.
            raise AccessDenied("policy principal unavailable")
        if type(contract_revision) is not int or contract_revision < 1 or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid policy revision")
        _names(required_context, "required context")
        if not isinstance(required_authority, dict) or set(required_authority) - set(required_context) or not set(required_authority.values()) <= AUTHORITIES:
            raise ValueError("invalid authority policy")
        if not isinstance(required_artifacts, tuple) or any(not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str) or not item[0] or type(item[1]) is not int or item[1] < 1 for item in required_artifacts):
            raise ValueError("invalid required artifacts")
        payload = dict(required_context=sorted(required_context), required_authority=dict(sorted(required_authority.items())),
                       required_artifacts=[list(item) for item in required_artifacts])
        with self._db() as db:
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM policies WHERE project=? AND contract_revision=? AND operation=?", (project, contract_revision, operation)).fetchone()[0]
            prior = db.execute("SELECT owner FROM policies WHERE project=? AND contract_revision=? AND operation=? ORDER BY revision LIMIT 1", (project, contract_revision, operation)).fetchone()
            if current != expected_revision:
                raise ValueError("revision conflict")
            if prior and prior["owner"] != actor:
                raise AccessDenied("policy unavailable")
            revision = current + 1
            db.execute("INSERT INTO policies VALUES(?,?,?,?,?,?,?)", (project, contract_revision, operation, revision, actor, principal, json.dumps(payload, ensure_ascii=False)))
            return revision

    def policy(self, project, actor, contract_revision, operation) -> OperationPolicy:
        with self._db() as db:
            row = db.execute("""SELECT * FROM policies WHERE project=? AND contract_revision=? AND operation=?
                ORDER BY revision DESC LIMIT 1""", (project, contract_revision, operation)).fetchone()
        if row is None or row["owner"] != actor:
            raise InsufficientContext("completion policy unavailable")
        payload = json.loads(row["payload"])
        return OperationPolicy(project, contract_revision, operation, row["revision"], row["principal"],
            frozenset(payload["required_context"]), dict(payload["required_authority"]),
            tuple((item[0], item[1]) for item in payload["required_artifacts"]))

    def validate_completion(self, project, actor, contract_revision, output, artifacts: ArtifactStore, *, operation="finish"):
        policy = self.policy(project, actor, contract_revision, operation)
        # The policy principal is an explicit capability boundary and must not be substituted.
        if policy.principal != actor:
            raise InsufficientContext("completion policy principal unavailable")
        self.resolve(project, actor, operation, required=policy.required_context, required_authority=policy.required_authority)
        access = Access(actor, project)
        for artifact_id, revision in policy.required_artifacts:
            with artifacts.open_verified(artifact_id, revision, access):
                pass
        with artifacts.open_verified(output["artifact_id"], output["revision"], access) as (manifest, _):
            if manifest["sha256"] != output["sha256"]:
                raise InsufficientContext("output identity mismatch")

    def export(self, project: str, actor: str) -> dict:
        """Owner-only machine-readable export preserving old source/context/policy revisions."""
        with self._db() as db:
            sources = [dict(row) for row in db.execute("SELECT * FROM sources WHERE project=? AND owner=? ORDER BY id,revision", (project, actor))]
            contexts = []
            for row in db.execute("SELECT * FROM contexts WHERE project=? AND owner=? ORDER BY id,revision", (project, actor)):
                entry = dict(row)
                for table, key in (("context_projects", "projects"), ("context_operations", "operations"), ("context_readers", "readers")):
                    entry[key] = [value[0] for value in db.execute(f"SELECT value FROM {table} WHERE id=? AND revision=? ORDER BY value", (row["id"], row["revision"]))]
                contexts.append(entry)
            policies = []
            for row in db.execute("SELECT * FROM policies WHERE project=? AND owner=? ORDER BY contract_revision,operation,revision", (project, actor)):
                entry = dict(row)
                entry["payload"] = json.loads(entry["payload"])
                policies.append(entry)
        return dict(schema="nexus.context.v1", sources=sources, contexts=contexts, policies=policies)

    def restore_export(self, snapshot: dict, actor: str) -> None:
        """Restore a verified owner export into an empty ContextStore only."""
        if not isinstance(snapshot, dict) or snapshot.get("schema") != "nexus.context.v1":
            raise ValueError("unsupported context export")
        if not all(isinstance(snapshot.get(key), list) for key in ("sources", "contexts", "policies")):
            raise ValueError("invalid context export")
        with self._db() as db:
            if db.execute("SELECT 1 FROM sources UNION SELECT 1 FROM contexts UNION SELECT 1 FROM policies LIMIT 1").fetchone():
                raise FileExistsError("context store is not empty")
        sources = {}
        for item in snapshot["sources"]:
            if set(item) != {"id", "revision", "project", "owner", "body", "sha256"} or item["owner"] != actor:
                raise ValueError("invalid source export")
            ref = self.register_source(item["id"], item["project"], actor, item["body"], expected_revision=item["revision"] - 1)
            if ref.revision != item["revision"] or ref.sha256 != item["sha256"]:
                raise InsufficientContext("source restore mismatch")
            sources[(ref.id, ref.revision)] = ref
        methods = {("decision", "confirmed"): self.confirm_decision, ("personal", "confirmed"): self.save_personal_context,
                   ("reference", "verified"): self.verify_reference, ("reference", "derived"): self.derive,
                   ("candidate", "candidate"): self.candidate}
        for item in snapshot["contexts"]:
            expected = {"id", "revision", "project", "owner", "kind", "authority", "status", "source_id", "source_revision", "body", "approval_receipt", "projects", "operations", "readers"}
            if set(item) != expected or item["owner"] != actor or (item["kind"], item["authority"]) not in methods:
                raise ValueError("invalid context export")
            source = sources.get((item["source_id"], item["source_revision"]))
            if source is None:
                raise InsufficientContext("context source unavailable")
            writer = methods[(item["kind"], item["authority"])]
            options = dict(source=source, body=item["body"], projects=frozenset(item["projects"]), operations=frozenset(item["operations"]),
                readers=frozenset(item["readers"]), expected_revision=item["revision"] - 1, status=item["status"])
            if writer == self.confirm_decision:
                options["approval_receipt"] = item["approval_receipt"]
            elif item["approval_receipt"] is not None:
                raise ValueError("invalid context approval export")
            revision = writer(item["id"], item["project"], actor,
                **options)
            if revision != item["revision"]:
                raise InsufficientContext("context restore mismatch")
        for item in snapshot["policies"]:
            expected = {"project", "contract_revision", "operation", "revision", "owner", "principal", "payload"}
            if set(item) != expected or item["owner"] != actor or not isinstance(item["payload"], dict):
                raise ValueError("invalid policy export")
            payload = item["payload"]
            revision = self.set_operation_policy(item["project"], actor, contract_revision=item["contract_revision"],
                operation=item["operation"], principal=item["principal"], required_context=frozenset(payload.get("required_context", [])),
                required_authority=payload.get("required_authority"), required_artifacts=tuple(tuple(value) for value in payload.get("required_artifacts", [])),
                expected_revision=item["revision"] - 1)
            if revision != item["revision"]:
                raise InsufficientContext("policy restore mismatch")
