"""Provider-neutral artifact identity with immutable revisions and verified reads.

This increment is not a complete context engine or multi-user service.
The caller must derive Access from trusted server configuration/authentication.
"""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import codecs
import io
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import BinaryIO, Protocol

CHUNK_BYTES = 1024 * 1024
MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
MAX_INLINE_BYTES = 4 * 1024 * 1024
DEFAULT_STORAGE_BYTES = 90_000_000_000


class InsufficientContext(Exception):
    """Required original is unavailable or inconsistent; never substitute."""


class AccessDenied(Exception):
    """Do not disclose existence or metadata to an unauthorized client."""


@dataclass(frozen=True)
class Access:
    principal: str
    project: str


class BinaryProvider(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, locator: str) -> bytes: ...
    def put_stream(self, source: BinaryIO, *, max_bytes: int,
                   storage_bytes: int, mime: str) -> tuple[str, str, int]: ...
    def open(self, locator: str) -> BinaryIO: ...


class LocalBinaryProvider:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, locator: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", locator) is None:
            raise InsufficientContext("invalid binary locator")
        path = self.root / locator
        if path.resolve().parent != self.root:
            raise InsufficientContext("binary outside provider boundary")
        return path

    def put(self, data: bytes) -> str:
        return self.put_stream(io.BytesIO(data), max_bytes=MAX_ORIGINAL_BYTES,
                               storage_bytes=DEFAULT_STORAGE_BYTES, mime="application/octet-stream")[0]

    def put_stream(self, source: BinaryIO, *, max_bytes: int,
                   storage_bytes: int, mime: str) -> tuple[str, str, int]:
        """Stage, fsync, then publish without overwriting an immutable blob.

        ArtifactStore holds its SQLite writer lock across this operation. All
        writers sharing this directory must use that same manifest database.
        """
        decoder = codecs.getincrementaldecoder("utf-8")() if mime == "text/plain" else None
        used = sum(p.stat().st_size for p in self.root.iterdir()
                   if re.fullmatch(r"[0-9a-f]{64}", p.name) and p.is_file())
        size = 0
        digest = hashlib.sha256()
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("original exceeds configured size limit")
                    # Also reserve temporary disk space, including duplicate uploads.
                    if used + size > storage_bytes:
                        raise ValueError("local storage budget exhausted")
                    if decoder:
                        decoder.decode(chunk)
                    digest.update(chunk)
                    target.write(chunk)
                if decoder:
                    decoder.decode(b"", final=True)
                target.flush()
                os.fsync(target.fileno())
            locator = digest.hexdigest()
            try:
                os.link(name, self._path(locator))
            except FileExistsError:
                with self.open(locator) as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() != locator:
                        raise InsufficientContext("stored binary is corrupt")
            return locator, locator, size
        finally:
            Path(name).unlink(missing_ok=True)

    def open(self, locator: str) -> BinaryIO:
        try:
            return self._path(locator).open("rb")
        except OSError as exc:
            raise InsufficientContext("original binary unavailable") from exc

    def get(self, locator: str) -> bytes:
        try:
            return self._path(locator).read_bytes()
        except OSError as exc:
            raise InsufficientContext("original binary unavailable") from exc


@dataclass(frozen=True)
class Original:
    artifact_id: str
    revision: int
    project: str
    mime_type: str
    sha256: str
    size: int
    data: bytes

    def manifest(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "data"}


class ArtifactStore:
    def __init__(self, database: Path, provider: BinaryProvider, *,
                 max_original_bytes: int = MAX_ORIGINAL_BYTES,
                 storage_bytes: int = DEFAULT_STORAGE_BYTES):
        self.database = database
        self.provider = provider
        if not 0 < max_original_bytes <= MAX_ORIGINAL_BYTES or storage_bytes <= 0:
            raise ValueError("invalid storage limits")
        self.max_original_bytes = max_original_bytes
        self.storage_bytes = storage_bytes
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL REFERENCES artifacts(id),
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    mime TEXT NOT NULL, digest TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0), locator TEXT NOT NULL,
                    PRIMARY KEY(id, revision));
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT NOT NULL REFERENCES artifacts(id), principal TEXT NOT NULL,
                    PRIMARY KEY(id, principal));
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def register(self, artifact_id: str, project: str, data: bytes, mime: str,
                 principals: tuple[str, ...], *, expected_revision: int = 0) -> int:
        return self.register_stream(artifact_id, project, io.BytesIO(data), mime,
                                    principals, expected_revision=expected_revision)

    def register_stream(self, artifact_id: str, project: str, source: BinaryIO, mime: str,
                        principals: tuple[str, ...], *, expected_revision: int = 0) -> int:
        """Trusted local writer, deliberately not exposed as an MCP tool.

        Optimistic concurrency prevents silently overwriting another writer.
        Permission changes for existing artifacts are not supported here.
        """
        if not artifact_id or not project or not principals or not all(principals):
            raise ValueError("identity, project and explicit readers required")
        if mime not in {"image/png", "text/plain", "application/pdf", "application/zip",
                        "application/octet-stream"}:
            raise ValueError("unsupported initial-slice MIME type")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT project FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM revisions WHERE id=?", (artifact_id,)).fetchone()[0]
            if current != expected_revision:
                raise ValueError("revision conflict")
            if old:
                readers = {row[0] for row in db.execute("SELECT principal FROM grants WHERE id=?", (artifact_id,))}
                if old[0] != project or readers != set(principals):
                    raise ValueError("scope or permission change requires separate authorization")
            else:
                db.execute("INSERT INTO artifacts VALUES (?,?)", (artifact_id, project))
                db.executemany("INSERT INTO grants VALUES (?,?)", [(artifact_id, p) for p in set(principals)])
            locator, digest, size = self.provider.put_stream(source, max_bytes=self.max_original_bytes,
                                                    storage_bytes=self.storage_bytes, mime=mime)
            revision = current + 1
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", (
                artifact_id, revision, mime, digest, size, locator))
        return revision

    def describe(self, artifact_id: str, revision: int, access: Access) -> dict:
        return self._metadata(artifact_id, revision, access)[0]

    def _metadata(self, artifact_id: str, revision: int, access: Access) -> tuple[dict, str]:
        with self._connect() as db:
            allowed = db.execute("""SELECT 1 FROM artifacts a JOIN grants g ON a.id=g.id
                WHERE a.id=? AND a.project=? AND g.principal=?""",
                (artifact_id, access.project, access.principal)).fetchone()
            if not allowed:
                raise AccessDenied("artifact not accessible")
            row = db.execute("SELECT mime,digest,size,locator FROM revisions WHERE id=? AND revision=?",
                             (artifact_id, revision)).fetchone()
        if row is None:
            raise InsufficientContext("required revision unavailable")
        mime, digest, size, locator = row
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not 0 <= size <= self.max_original_bytes:
            raise InsufficientContext("invalid original manifest")
        return dict(artifact_id=artifact_id, revision=revision, project=access.project,
                    mime_type=mime, sha256=digest, size=size), locator

    @contextmanager
    def open_verified(self, artifact_id: str, revision: int, access: Access):
        """Verify into a disk-backed snapshot before releasing any original bytes.

        The verified snapshot prevents later changes to the source file from
        changing the response. This is bounded RAM, not a partial-hash promise.
        """
        manifest, locator = self._metadata(artifact_id, revision, access)
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            digest, size = hashlib.sha256(), 0
            with self.provider.open(locator) as source:
                while chunk := source.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > manifest["size"]:
                        raise InsufficientContext("original integrity mismatch")
                    digest.update(chunk)
                    snapshot.write(chunk)
            if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                raise InsufficientContext("original integrity mismatch")
            snapshot.seek(0)
            yield manifest, snapshot

    def read(self, artifact_id: str, revision: int, access: Access, *, max_bytes=MAX_INLINE_BYTES) -> Original:
        if not 0 < max_bytes <= MAX_ORIGINAL_BYTES:
            raise ValueError("invalid inline limit")
        manifest = self.describe(artifact_id, revision, access)
        if manifest["size"] > max_bytes:
            raise InsufficientContext("original exceeds inline transport limit; verified stream required")
        with self.open_verified(artifact_id, revision, access) as (manifest, stream):
            return Original(**manifest, data=stream.read())
