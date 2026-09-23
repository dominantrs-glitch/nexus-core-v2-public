"""Reviewable local native-Core packages. No network or canonical-route activation.

Prepare leaves the source live. Freeze is an explicit second step after reviewing
the exact project/package. A failed export never freezes the source. An uncertain
freeze can be retried; it never thaws or silently chooses a destination writer.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from nexus.artifacts import InsufficientContext
from nexus.core_backup import export_core, restore_core
from nexus.core_freeze import freeze_core, inspect_core
from nexus.owner import ApprovalLedger


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _files(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise InsufficientContext('migration package link forbidden')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _hash(path)
    return result


def _write_new(path, value):
    handle, temporary = tempfile.mkstemp(prefix='.nexus-preparing-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic and exclusive: never expose a partial ready/frozen receipt or
        # replace an existing package. All supported local stores use NTFS here.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _verify_restore(bundle, owner):
    # The verifier gets a new independent directory, never the live workspace.
    with tempfile.TemporaryDirectory(prefix='nexus-core-verify-') as temporary:
        root = Path(temporary) / 'restored'
        projects, contexts, artifacts = restore_core(bundle, root, owner)
        project = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))['project']
        expected = json.loads((bundle / 'project.json').read_text(encoding='utf-8'))
        if projects.export(project, owner) != expected:
            raise InsufficientContext('restored project differs')
        if contexts.export(project, owner) != json.loads((bundle / 'context.json').read_text(encoding='utf-8')):
            raise InsufficientContext('restored context differs')
        audit = ApprovalLedger(root / 'approvals.sqlite3').export_project(owner, project)
        if audit != json.loads((bundle / 'approvals.json').read_text(encoding='utf-8')):
            raise InsufficientContext('restored confirmation audit differs')
        # A second export checks every restored artifact version, grant and byte.
        second = Path(temporary) / 'second'
        export_core(projects, contexts, artifacts, project, owner, second,
                    approvals=ApprovalLedger(root / 'approvals.sqlite3'))
        if _files(second) != _files(bundle):
            raise InsufficientContext('restored package differs')


def prepare_core(task, destination):
    """Export just this project and verify a byte-identical independent restore."""
    before = inspect_core(task)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    bundle = destination / 'bundle'
    export_core(task.projects, task.contexts, task.artifacts, task.project, task.owner, bundle,
                approvals=task.adapter.approvals)
    _verify_restore(bundle, task.owner)
    after = inspect_core(task)
    if before != after:
        raise InsufficientContext('native source changed during preparation; prepare a new package')
    review = dict(format='nexus-core-migration-v1', owner=task.owner, **before, files=_files(bundle))
    # Written last: a partial directory never counts as a prepared package.
    _write_new(destination / 'package.json', review)
    return review


def verify_package(destination):
    """Validate local transport bytes and semantics, without asserting live state."""
    destination = Path(destination)
    path = destination / 'package.json'
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise InsufficientContext('migration review unavailable')
    review = json.loads(path.read_text(encoding='utf-8'))
    if (set(review) != {'format', 'owner', 'project', 'contract_revision', 'source_digest', 'counts', 'files'}
            or review['format'] != 'nexus-core-migration-v1'
            or not isinstance(review['owner'], str) or not isinstance(review['project'], str)
            or not isinstance(review['files'], dict)):
        raise InsufficientContext('unsupported migration package')
    bundle = destination / 'bundle'
    if bundle.is_symlink() or _files(bundle) != review['files']:
        raise InsufficientContext('migration package changed')
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['format'] != 'nexus-core-v2' or manifest['project'] != review['project']:
        raise InsufficientContext('migration package project mismatch')
    _verify_restore(bundle, review['owner'])
    return review


def freeze_prepared_core(task, destination):
    """Explicit reviewed-source freeze only. Does not activate the packaged copy."""
    destination = Path(destination)
    review = verify_package(destination)
    if review['project'] != task.project or review['owner'] != task.owner:
        raise InsufficientContext('migration source identity mismatch')
    # Hashes on a mutable package are integrity checks, not an attestation of
    # its source. Re-export the selected live/frozen source and compare exact
    # bytes before freezing; the source digest check also catches concurrent edits.
    with tempfile.TemporaryDirectory(prefix='nexus-core-source-') as temporary:
        bundle = Path(temporary) / 'bundle'
        export_core(task.projects, task.contexts, task.artifacts, task.project, task.owner, bundle,
                    approvals=task.adapter.approvals)
        if _files(bundle) != review['files']:
            raise InsufficientContext('migration package does not match source')
    frozen = freeze_core(task, review['source_digest'])
    result = dict(format='nexus-core-frozen-package-v1', **frozen, package_sha256=_hash(destination / 'package.json'))
    path = destination / 'frozen.json'
    if path.exists():
        if path.is_symlink() or json.loads(path.read_text(encoding='utf-8')) != result:
            raise InsufficientContext('frozen package receipt conflict; source stays frozen')
    else:
        _write_new(path, result)
    return result
