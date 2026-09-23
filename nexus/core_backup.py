"""Project-scoped portable Core export/restore; local files only."""
import hashlib
import json
from pathlib import Path

from nexus.artifacts import ArtifactStore, LocalBinaryProvider, InsufficientContext
from nexus.backup import export_bundle, restore_bundle
from nexus.context_store import ContextStore
from nexus.owner import ApprovalLedger
from nexus.project import ProjectStore


def _read_json(path: Path, limit=16 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError("export metadata limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def export_core(projects: ProjectStore, contexts: ContextStore, artifacts: ArtifactStore, project: str, actor: str, destination: Path, *, approvals: ApprovalLedger | None = None):
    """Create a new project-scoped bundle; optional receipts contain no capability secret."""
    destination.mkdir(parents=True, exist_ok=False)
    project_path, context_path = destination / "project.json", destination / "context.json"
    project_path.write_text(json.dumps(projects.export(project, actor), ensure_ascii=False, indent=2), encoding="utf-8")
    context_path.write_text(json.dumps(contexts.export(project, actor), ensure_ascii=False, indent=2), encoding="utf-8")
    count = export_bundle(artifacts, destination / "artifacts", project=project)
    manifest_path = destination / "artifacts" / "manifest.json"
    files = {"project.json": _hash(project_path), "context.json": _hash(context_path),
             "artifacts/manifest.json": _hash(manifest_path)}
    bundle_format = "nexus-core-v1"
    if approvals is not None:
        approval_path = destination / "approvals.json"
        approval_path.write_text(json.dumps(approvals.export_project(actor, project), ensure_ascii=False, indent=2), encoding="utf-8")
        files["approvals.json"] = _hash(approval_path)
        bundle_format = "nexus-core-v2"
    manifest = {"format": bundle_format, "project": project, "files": files}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def restore_core(source: Path, destination: Path, actor: str):
    """Restore only into a fresh directory; originals and source export stay untouched."""
    manifest = _read_json(source / "manifest.json")
    formats = {
        "nexus-core-v1": {"project.json", "context.json", "artifacts/manifest.json"},
        "nexus-core-v2": {"project.json", "context.json", "artifacts/manifest.json", "approvals.json"},
    }
    if (set(manifest) != {"format", "project", "files"} or manifest.get("format") not in formats
            or not isinstance(manifest["project"], str) or set(manifest["files"]) != formats[manifest["format"]]):
        raise ValueError("unsupported core export")
    for relative, digest in manifest["files"].items():
        path = source / relative
        if not isinstance(digest, str) or path.is_symlink() or path.resolve().parent not in {source.resolve(), (source / "artifacts").resolve()} or _hash(path) != digest:
            raise InsufficientContext("core export integrity mismatch")
    destination.mkdir(parents=True, exist_ok=False)
    restore_bundle(source / "artifacts", destination / "artifacts")
    artifacts = ArtifactStore(destination / "artifacts" / "manifest.sqlite3", LocalBinaryProvider(destination / "artifacts" / "blobs"))
    contexts = ContextStore(destination / "context.sqlite3")
    contexts.restore_export(_read_json(source / "context.json"), actor)
    projects = ProjectStore(destination / "projects.sqlite3", context_store=contexts, artifact_store=artifacts)
    snapshot = _read_json(source / "project.json")
    if snapshot.get("project", {}).get("id") != manifest["project"]:
        raise InsufficientContext("core export project mismatch")
    projects.restore_export(snapshot, actor, artifacts)
    if manifest["format"] == "nexus-core-v2":
        ApprovalLedger(destination / "approvals.sqlite3").restore_project(
            _read_json(source / "approvals.json"), actor, manifest["project"])
    return projects, contexts, artifacts
