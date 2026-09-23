"""Portable local export/restore. No cloud sync, encryption, or credential export."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from nexus.artifacts import ArtifactStore, LocalBinaryProvider, CHUNK_BYTES, InsufficientContext


def copy_verified(source, target, size, digest):
    count, checksum = 0, hashlib.sha256()
    with target.open("xb") as output:
        while chunk := source.read(CHUNK_BYTES):
            count += len(chunk)
            if count > size:
                raise InsufficientContext("backup size mismatch")
            checksum.update(chunk)
            output.write(chunk)
    if count != size or checksum.hexdigest() != digest:
        raise InsufficientContext("backup integrity mismatch")


def export_bundle(store: ArtifactStore, destination: Path, *, project: str | None = None):
    """Trusted local operator export. Incomplete folders have no manifest.json."""
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "blobs").mkdir()
    with store._connect() as db:
        # One writer boundary for metadata, permissions, and immutable originals.
        db.execute("BEGIN IMMEDIATE")
        entries = []
        copied = {}
        query = "SELECT a.id,a.project,r.revision,r.mime,r.digest,r.size,r.locator FROM artifacts a JOIN revisions r ON a.id=r.id"
        args = () if project is None else (project,)
        if project is not None:
            query += " WHERE a.project=?"
        query += " ORDER BY a.id,r.revision"
        for artifact, item_project, revision, mime, digest, size, locator in db.execute(query, args):
            readers = [r[0] for r in db.execute("SELECT principal FROM grants WHERE id=? ORDER BY principal", (artifact,))]
            if not readers or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise InsufficientContext("invalid export metadata")
            if digest not in copied:
                with store.provider.open(locator) as source:
                    copy_verified(source, destination / "blobs" / digest, size, digest)
                copied[digest] = size
            elif copied[digest] != size:
                raise InsufficientContext("conflicting backup metadata")
            entries.append(dict(artifact_id=artifact, project=item_project, revision=revision,
                                mime=mime, sha256=digest, size=size, principals=readers))
        manifest = {"format": "nexus-artifacts-v1", "entries": entries}
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(entries)


def restore_bundle(source: Path, destination: Path):
    """Restore into a new directory; never merge into or overwrite a live store.

    A failure leaves an explicit partial directory for inspection, without a
    manifest.sqlite3 entry point. Original backups are never modified.
    """
    path = source / "manifest.json"
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("backup manifest limit")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != "nexus-artifacts-v1" or not isinstance(manifest.get("entries"), list):
        raise ValueError("unsupported backup")
    destination.mkdir(parents=True, exist_ok=False)
    database = destination / "restore.pending.sqlite3"
    store = ArtifactStore(database, LocalBinaryProvider(destination / "blobs"))
    for item in manifest["entries"]:
        if set(item) != {"artifact_id", "project", "revision", "mime", "sha256", "size", "principals"}:
            raise ValueError("invalid backup entry")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("invalid backup digest")
        blob = source / "blobs" / item["sha256"]
        if blob.is_symlink() or blob.resolve().parent != (source / "blobs").resolve():
            raise ValueError("backup path outside boundary")
        with blob.open("rb") as stream:
            # Validate before any committed revision. register_stream verifies UTF-8 too.
            if blob.stat().st_size != item["size"] or hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise InsufficientContext("backup integrity mismatch")
            stream.seek(0)
            revision = store.register_stream(item["artifact_id"], item["project"], stream, item["mime"],
                                            tuple(item["principals"]), expected_revision=item["revision"] - 1)
            with store._connect() as db:
                actual = db.execute("SELECT digest,size FROM revisions WHERE id=? AND revision=?",
                                    (item["artifact_id"], revision)).fetchone()
                if actual != (item["sha256"], item["size"]):
                    raise InsufficientContext("backup changed during restore")
    database.rename(destination / "manifest.sqlite3")
    return len(manifest["entries"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "restore"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    if args.operation == "restore":
        count = restore_bundle(args.source, args.destination)
    else:
        if not (args.source / "manifest.sqlite3").is_file():
            parser.error("existing manifest required")
        store = ArtifactStore(args.source / "manifest.sqlite3", LocalBinaryProvider(args.source / "blobs"))
        count = export_bundle(store, args.destination)
    print(f"{args.operation}: {count} artifact revisions verified; local only")


if __name__ == "__main__":
    main()
